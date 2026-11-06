CREATE TABLE drawio_nodes (
    id uuid PRIMARY KEY,
    owner text NOT NULL,
    xml text NOT NULL,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
