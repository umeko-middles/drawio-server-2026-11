# Doc Master Draw.io

把 `src/drawio/` 合入现有的 Python 3.12 / FastAPI 服务。FastAPI 从 OBS 预加载并托管固定版本的 Draw.io Web 静态资源。浏览器完成编辑和 PNG/SVG 导出，PostgreSQL 保存画布 XML 与版本。生产 Linux x86_64 可直接用本仓库的静态包，包与 CPU 架构无关，服务端也没有新增 npm 或 PyPI 依赖。

## 新增接口

以下路径均在 `/api/doc-master/v1/drawio` 下。画布和校验接口复用宿主 SSO Cookie。每个画布归属创建者，用户标识须包含租户范围（如有）。访问他人的画布返回 `404`。`POST`、`PUT`、`DELETE` 要求 `Origin` 与 `public_origin` 精确匹配。浏览器同源请求会自动设置该头。

| 方法 | 相对路径 | 输入与结果 |
|---|---|---|
| `GET` | `/editor` | 打开新建或导入画布的页面。 |
| `GET` | `/editor/{node_id}` | 打开指定画布。页面可导入、编辑、保存、下载 `.drawio`，以及导出当前页 PNG/SVG。 |
| `GET` | `/editor.js` | 页面使用的同源脚本，无业务数据。 |
| `GET` / `HEAD` | `/assets/{path}` | Draw.io 静态资源，无业务数据。编辑器入口为 `/assets/index.html`。 |
| `POST` | `/nodes` | 空请求体创建空白画布，或以 `application/xml` / `text/xml` 提交 Draw.io XML。返回 `201` 和画布元数据。 |
| `GET` | `/nodes/{node_id}` | 返回画布元数据和 `ETag`。编辑页约每 3 秒查询一次。 |
| `GET` | `/nodes/{node_id}/xml` | 返回已保存的 UTF-8 XML、`ETag` 和 `.drawio` 下载文件名。 |
| `PUT` | `/nodes/{node_id}/xml` | 以 XML 替换画布。要求 `If-Match`，成功后返回更新后的元数据和 `ETag`。 |
| `DELETE` | `/nodes/{node_id}` | 删除自己的画布。要求 `If-Match`，成功返回 `204`。 |
| `POST` | `/validate` | 提交 XML，成功返回 `{"pages": 2}`。检查 XML 结构、页面和压缩数据，供导入前使用。 |

`node_id` 为 UUID。元数据形如 `{"id":"24b08dea-109f-4e85-ad0f-7f1a0a38b6d7","version":1,"updated_at":"2026-11-06T08:00:00Z"}`，对应 `ETag: "1"`。更新和删除时原样传入 `If-Match: "1"`。PostgreSQL 在同一条 SQL 中校验 owner 与版本并写入，四个 Uvicorn worker 共用同一份状态。

| 状态码 | 含义 |
|---|---|
| `400` | `If-Match` 格式不正确，须为单个带双引号的正整数。 |
| `401` / `403` | 宿主认证失败、身份无效或写请求的 Origin 不匹配。 |
| `404` | 画布不存在或不属于当前用户。 |
| `412` | 版本已变化。先重新读取，再决定是否提交。 |
| `413` | XML 请求超过 4 MiB，或所有压缩页面解压后合计超过 16 MiB。 |
| `415` | 有内容的请求使用了非 XML 媒体类型。 |
| `422` | UUID 无效、XML 不合规、不是 Draw.io 画布或包含 DTD。 |
| `428` | 更新或删除缺少 `If-Match`。 |

支持单页 `<mxGraphModel>`，以及含压缩或未压缩页面的 `<mxfile>`。校验保留原 XML 内容，仅移除可选的 UTF-8 BOM。它检查文件结构，图形、字体和连线效果由编辑器解释。页面空闲且没有本地修改时会应用服务器更新。用户正在编辑或已有本地修改时，页面提示先下载本地副本，再选择是否载入服务器版本。

浏览器的 `.drawio`、PNG 和 SVG 导出使用当前编辑状态，包含尚未保存的修改。HTTP XML 接口读写的是已保存版本。已有会话的同一所有者可以用 `PUT` 推图。首版没有独立 Agent 凭据、MCP、无人值守 `/render` 或服务端 Visio/Mermaid/CSV 转换接口。

## 合入宿主

1. 复制 `src/drawio/`，将 [迁移 SQL](migrations/001_drawio_nodes.sql) 纳入宿主迁移。在数据库部署阶段执行一次。四个 worker 不各自运行 DDL。
2. 在 `settings.toml` 增加 `[drawio]`，通过宿主设置模块读取。OBS 凭据继续走既有 J2C 和 `obs_store`。

   ```toml
   [drawio]
   public_origin = "https://doc-master.example.internal"
   cache_dir = "/applog/drawio"
   object_key = "drawio/releases/drawio-web-31.4.5-r1.tar.gz"
   sha256 = "96eaa2b36dfdcb87a4f91730e1f8e788f8c3b5085a1276f4f11d09db5b48bbf0"
   ```

3. 在 `src/api/__init__.py` 注册子路由。下面的 `require_auth`、`pg_pool` 和字段名由内网 Agent 替换成宿主已有实现。`current_owner` 要返回稳定、唯一的 SSO 主体，按需加租户命名空间。`fetch_row` 约定 `await fetch_row(sql, *values)`，使用 PostgreSQL `$1` 绑定，返回映射或 `None`，更新和删除提交后再返回。若宿主驱动绑定方式不同，在这个接入点转换，保留参数化查询。

   ```python
   from fastapi import Depends
   from src.drawio.router import create_router

   async def current_owner(identity=Depends(require_auth)) -> str:
       return identity.employee_id  # 替换成真实的稳定 SSO 主体

   async def fetch_row(sql, *values):
       return await pg_pool.fetchrow(sql, *values)

   api_router.include_router(create_router(
       current_owner=current_owner,
       fetch_row=fetch_row,
       public_origin=settings.drawio.public_origin,
   ))
   ```

4. 在 `main.py` 挂载静态路径，并在既有 lifespan 的 `obs_store.initialize()` 成功后加载制品，之后才 `yield`。下面的 OBS 调用参数是接入示例。若 `get_object` 同步阻塞，用 `await asyncio.to_thread(...)`，并把结果转换成原始 `bytes`。

   ```python
   from pathlib import Path
   from src.drawio.assets import AssetSettings, EditorAssets, prepare_assets

   drawio_assets = AssetSettings(
       cache_dir=Path(settings.drawio.cache_dir),
       object_key=settings.drawio.object_key,
       sha256=settings.drawio.sha256,
   )
   app.mount(f"{API_V1_STR}/drawio/assets", EditorAssets(drawio_assets), name="drawio-assets")

   async def load_drawio_object(key: str) -> bytes:
       return await obs_store.get_object(key)

   # 添加在现有 lifespan 中 obs_store.initialize() 之后
   await prepare_assets(drawio_assets, load_drawio_object)
   ```

首次启动时，同一容器的四个 worker 以文件锁串行进入初始化，只有首个需要下载。SHA-256 核对后，制品在同一文件系统的临时目录解包并原子发布。缓存路径为 `/applog/drawio/<sha256>/web`。下载、校验或解包失败会阻止该 worker 启动，可查看启动日志。回滚时将 `object_key` 与 `sha256` 一并改回上一版，再重启。旧版本缓存由部署侧清理。

浏览器对宿主的 API、编辑页和静态资源必须同源。若网关或宿主统一设置了 `X-Frame-Options: DENY` 或 `frame-ancestors 'none'`，需在 Draw.io 路径放行同源 iframe。制品使用离线参数，页面策略限定为同源资源、内嵌图片与字体。外链素材应改用同源或内嵌资源。宿主继续提供原有 `/health`。

## 发布与验证

将 [静态包](release/drawio-web-31.4.5-r1.tar.gz) 上传到配置的 OBS 对象路径，将 [SHA-256 校验文件](release/drawio-web-31.4.5-r1.tar.gz.sha256) 中的摘要填入 `settings.toml`。上传的是 gzip 压缩包本身，无须预先解包。制品来自已安装的官方 Draw.io Desktop 31.4.5 的 `app.asar` 中所含 Web 编辑器，包含来源清单与许可证。离线覆盖在 `js/PreConfig.js` 和 `js/PostConfig.js` 中有注释。版本说明见 [Draw.io Desktop 31.4.5](https://github.com/jgraph/drawio-desktop/releases/tag/v31.4.5)。

```powershell
python scripts/package_drawio.py --asar 'C:\Program Files\draw.io\resources\app.asar' --revision 1
Get-FileHash release/drawio-web-31.4.5-r1.tar.gz -Algorithm SHA256
```

如用官方 Web 源码打包，在固定 release tag 的检出上运行 `python scripts/package_drawio.py --checkout /path/to/drawio --tag vX.Y.Z --revision 1`。保留整个 Web 资源目录及内附第三方声明，构建会排除 `META-INF` 和 `WEB-INF`。部署用摘要以本次构建产物为准。

内网验收应在生产同款 x86_64 镜像、四个 worker 和阻断公网的环境完成。用 SSO 新建画布，再导入有中文和多页的 `.drawio`，确认保存、刷新、页面选择和 PNG/SVG 下载。用两个请求携带同一 ETag 写入同一画布，确认只有一个成功。再用第二个 SSO 身份访问同一 ID，确认得到 `404`。
