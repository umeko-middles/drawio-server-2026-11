"use strict";

const ui = Object.fromEntries([...document.querySelectorAll("[id]")].map(element => [element.id, element]));
const base = new URL(document.body.dataset.base, location.href);
const id = document.body.dataset.base === "../" ? location.pathname.split("/").pop() : null;
const xmlPath = `nodes/${id}/xml`;
const xmlHeaders = { "Content-Type": "application/xml" };
let version = null;
let editCount = 0;
let dirty = false;
let ready = false;
let busy = false;
let polling = false;
let conflict = false;
let pendingExport = null;
let loading = null;
let lastInput = -Infinity;
let pointerDown = false;

function status(text) {
    ui.status.textContent = text;
}

function controls() {
    for (const name of ["save", "drawio", "png", "svg", "reload"]) {
        ui[name].disabled = !ready || busy || !!loading;
    }
    ui.import.disabled = busy || (id !== null && (!ready || !!loading));
    ui.create.disabled = busy;
    ui.reload.hidden = !conflict;
}

async function api(path, options = {}) {
    const response = await fetch(new URL(path, base), {
        ...options, cache: "no-store", credentials: "same-origin", redirect: "error",
    });
    if (!response.ok) {
        const payload = await response.json().catch(() => null);
        const failure = new Error(typeof payload?.detail === "string" ? payload.detail : `HTTP ${response.status}`);
        failure.status = response.status;
        throw failure;
    }
    return response;
}

function send(message) {
    ui.editor.contentWindow.postMessage(JSON.stringify(message), location.origin);
}

function load(xml, etag, unsaved = false) {
    loading = { etag, unsaved };
    controls();
    send({ action: "load", xml, autosave: 1, noExitBtn: 1 });
}

function localWorkInProgress() {
    const active = ui.editor.contentDocument?.activeElement;
    return dirty || pointerDown || performance.now() - lastInput < 500
        || active?.isContentEditable || active?.matches("input, textarea, select");
}

async function serverVersion({ automatic = false } = {}) {
    const response = await api(xmlPath);
    const xml = await response.text();
    if (automatic && (localWorkInProgress() || busy || loading)) {
        changed();
        return;
    }
    load(xml, response.headers.get("ETag"));
}

function changed() {
    conflict = true;
    status("服务器已有更新。请先下载本地修改，再加载服务器版本。");
    controls();
}

async function poll() {
    if (!ready || busy || polling || loading || conflict || document.hidden) return;
    polling = true;
    try {
        const response = await api(`nodes/${id}`);
        if (response.headers.get("ETag") !== version && !busy && !loading) {
            if (localWorkInProgress()) changed();
            else await serverVersion({ automatic: true });
        }
    } catch (error) {
        status(`检查服务器版本失败。${error.message}`);
    } finally {
        polling = false;
    }
}

async function run(action) {
    if (busy || loading) return;
    busy = true;
    controls();
    try {
        await action();
    } catch (error) {
        if (error.status === 412) changed();
        else status(error.message);
    } finally {
        busy = false;
        controls();
    }
}

async function save(xml, edits) {
    const response = await api(xmlPath, {
        method: "PUT", headers: { ...xmlHeaders, "If-Match": version }, body: xml,
    });
    version = response.headers.get("ETag");
    dirty = editCount !== edits;
    status(dirty ? "已保存。还有更新的本地修改。" : `已保存，版本 ${version.replaceAll('"', '')}`);
}

function exportDiagram(format) {
    return new Promise((resolve, reject) => {
        const requestId = crypto.randomUUID ? crypto.randomUUID() : String(performance.now());
        const timeout = setTimeout(() => {
            pendingExport = null;
            reject(new Error("编辑器导出超时"));
        }, 30000);
        pendingExport = {
            requestId,
            resolve: message => {
                clearTimeout(timeout);
                resolve(message);
            },
        };
        send({ action: "export", format, requestId, currentPage: true });
    });
}

function download(url, extension) {
    const link = document.createElement("a");
    link.href = url;
    link.download = `${id}.${extension}`;
    link.click();
}

async function saveCurrent() {
    const edits = editCount;
    const result = await exportDiagram("xml");
    if (typeof result.xml !== "string") throw new Error("编辑器没有返回 XML");
    await save(result.xml, edits);
}

function create(xml) {
    return run(async () => {
        const response = await api("nodes", { method: "POST", headers: xmlHeaders, body: xml });
        const node = await response.json();
        location.assign(new URL(`editor/${node.id}`, base));
    });
}

ui.create.onclick = () => create();
ui.import.onclick = () => ui.file.click();
ui.file.onchange = async () => {
    const file = ui.file.files[0];
    ui.file.value = "";
    if (!file) return;
    if (file.size > 4 * 1024 * 1024) {
        status("Draw.io 文件最大支持 4 MiB。");
        return;
    }
    if (!id) {
        await create(file);
        return;
    }
    if (dirty && !confirm("用导入文件替换未保存的修改？")) return;
    await run(async () => {
        const edits = editCount;
        const xml = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
        await api("validate", { method: "POST", headers: xmlHeaders, body: xml });
        if (editCount !== edits) throw new Error("校验期间画布已修改，请重新导入。");
        load(xml, version, true);
    });
};
ui.save.onclick = () => run(saveCurrent);
ui.reload.onclick = () => {
    if (!dirty || confirm("丢弃本地修改并加载服务器版本？")) {
        run(async () => {
            const edits = editCount;
            const response = await api(xmlPath);
            const xml = await response.text();
            if (editCount !== edits) throw new Error("加载期间画布已修改，请重新加载。");
            load(xml, response.headers.get("ETag"));
        });
    }
};
ui.drawio.onclick = () => run(async () => {
    const result = await exportDiagram("xml");
    if (typeof result.xml !== "string") throw new Error("编辑器没有返回 XML");
    const url = URL.createObjectURL(new Blob([result.xml], { type: "application/xml" }));
    download(url, "drawio");
    setTimeout(() => URL.revokeObjectURL(url), 1000);
});
for (const format of ["png", "svg"]) {
    ui[format].onclick = () => run(async () => {
        const result = await exportDiagram(format);
        const pattern = format === "png" ? /^data:image\/png;base64,/ : /^data:image\/svg\+xml[;,]/;
        if (typeof result.data !== "string" || !pattern.test(result.data)) {
            throw new Error("编辑器没有返回有效图像");
        }
        download(result.data, format);
    });
}

window.addEventListener("beforeunload", event => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = true;
});

window.addEventListener("message", event => {
    if (event.origin !== location.origin || event.source !== ui.editor.contentWindow || typeof event.data !== "string") return;
    let message;
    try {
        message = JSON.parse(event.data);
    } catch {
        return;
    }
    switch (message?.event) {
        case "configure":
            send({ action: "configure", config: { suppressNewWindows: true } });
            break;
        case "init":
            run(() => serverVersion());
            break;
        case "load":
            if (!loading) break;
            ({ etag: version, unsaved: dirty } = loading);
            loading = null;
            if (!dirty) conflict = false;
            ready = true;
            controls();
            status(dirty ? "已导入，尚未保存。" : `已加载，版本 ${version.replaceAll('"', '')}`);
            break;
        case "autosave":
            if (!ready || loading) break;
            editCount += 1;
            dirty = true;
            if (!conflict && !busy) status("有未保存的修改");
            break;
        case "save":
            if (ready && typeof message.xml === "string") run(() => save(message.xml, editCount));
            break;
        case "export":
            if (pendingExport && message.message?.requestId === pendingExport.requestId) {
                pendingExport.resolve(message);
                pendingExport = null;
            }
            break;
    }
});

if (id) {
    ui.editor.addEventListener("load", () => {
        const doc = ui.editor.contentDocument;
        if (!doc) return;
        doc.addEventListener("pointerdown", () => { pointerDown = true; }, true);
        const endPointer = () => { pointerDown = false; lastInput = performance.now(); };
        doc.addEventListener("pointerup", endPointer, true);
        doc.addEventListener("pointercancel", endPointer, true);
        doc.documentElement.addEventListener("pointerleave", endPointer);
        doc.addEventListener("keydown", () => { lastInput = performance.now(); }, true);
    });
    ui.editor.hidden = false;
    ui.editor.src = new URL("assets/index.html?embed=1&proto=json&configure=1&offline=1&stealth=1&pwa=0&noExitBtn=1&libraries=1&lang=zh", base);
    status("正在启动编辑器…");
    setTimeout(() => { if (!ready && !busy) status("编辑器未能加载，请刷新或联系管理员。"); }, 20000);
    setInterval(poll, 3000);
} else {
    ui.create.hidden = false;
    status("新建空白画布，或导入已有的 Draw.io 文件。");
}
controls();
