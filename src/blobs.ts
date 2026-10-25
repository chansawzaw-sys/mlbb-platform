// Local-disk BlobClient implementing the @hatch/space-sdk BlobClient
// contract (put / getUrl / delete / head / list), mirroring the Muse
// runtime worker's semantics:
//   - objects stored content-addressed under <root>/objects/<sha256(key)>
//   - metadata kept in a small bun:sqlite index (<root>/blobs-index.db),
//     the same columns the runtime worker uses
//   - getUrl(key) returns "./blobs/<base64url(key)>" — the standalone
//     server serves those bytes itself at GET /blobs/<token>
import { Database } from "bun:sqlite";
import { createHash } from "node:crypto";
import { mkdirSync } from "node:fs";
import { dirname, join, resolve, sep } from "node:path";

export type BlobPutData = string | ArrayBuffer | ArrayBufferView | Blob;
export interface BlobPutOptions {
  readonly contentType?: string;
  readonly public?: boolean;
}
export interface BlobUrlOptions {
  readonly expiresInSeconds?: number;
  readonly public?: boolean;
}
export interface BlobMetadata {
  readonly key: string;
  readonly contentType: string;
  readonly size: number;
  readonly sizeBytes: number;
  readonly etag: string;
  readonly visibility: "private" | "public";
  readonly createdAtMs: number;
  readonly updatedAtMs: number;
  readonly public: boolean;
}

const DEFAULT_CONTENT_TYPE = "application/octet-stream";

function validateKey(key: string): string {
  const trimmed = key.trim();
  if (!trimmed) throw new Error("ctx.blobs requires a non-empty key");
  if (trimmed.startsWith("/") || trimmed.includes("\\")) {
    throw new Error("ctx.blobs keys must be relative POSIX paths");
  }
  const parts = trimmed.split("/");
  if (parts.some((p) => p === "" || p === "." || p === "..")) {
    throw new Error("ctx.blobs keys cannot contain empty or dot path segments");
  }
  return trimmed;
}

function validatePrefix(prefix: string): string {
  if (prefix === "") return "";
  return validateKey(prefix);
}

function objectIdForKey(key: string): string {
  return createHash("sha256").update(key, "utf8").digest("hex");
}

function etagForBytes(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

export function encodeBlobKeyToken(key: string): string {
  return Buffer.from(key, "utf8").toString("base64url");
}

export function decodeBlobKeyToken(token: string): string | null {
  try {
    const key = Buffer.from(token, "base64url").toString("utf8");
    return validateKey(key);
  } catch {
    return null;
  }
}

async function blobDataToBytes(data: BlobPutData): Promise<Uint8Array> {
  if (typeof data === "string") return new TextEncoder().encode(data);
  if (data instanceof Blob) return new Uint8Array(await data.arrayBuffer());
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
}

interface BlobRow {
  key: string;
  content_type: string;
  size_bytes: number;
  etag: string;
  visibility: string;
  created_at_ms: number;
  updated_at_ms: number;
  object_id: string;
}

function metadataFromRow(row: BlobRow): BlobMetadata {
  const isPublic = row.visibility === "public";
  return {
    key: row.key,
    contentType: row.content_type,
    size: row.size_bytes,
    sizeBytes: row.size_bytes,
    etag: row.etag,
    visibility: isPublic ? "public" : "private",
    createdAtMs: row.created_at_ms,
    updatedAtMs: row.updated_at_ms,
    public: isPublic,
  };
}

export class LocalBlobStore {
  readonly root: string;
  private readonly index: Database;

  constructor(root: string) {
    this.root = resolve(root);
    mkdirSync(join(this.root, "objects"), { recursive: true });
    this.index = new Database(join(this.root, "blobs-index.db"));
    this.index.exec("PRAGMA journal_mode = WAL");
    this.index.exec(`
      CREATE TABLE IF NOT EXISTS blobs (
        key TEXT PRIMARY KEY,
        content_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        etag TEXT NOT NULL,
        visibility TEXT NOT NULL,
        created_at_ms INTEGER NOT NULL,
        updated_at_ms INTEGER NOT NULL,
        object_id TEXT NOT NULL
      )
    `);
  }

  private objectPathForId(objectId: string): string {
    const base = join(this.root, "objects");
    const p = resolve(join(base, objectId.slice(0, 2), objectId));
    if (!p.startsWith(base + sep)) throw new Error("blob path escapes store root");
    return p;
  }

  private row(key: string): BlobRow | null {
    return (
      this.index
        .query(
          "SELECT key, content_type, size_bytes, etag, visibility, created_at_ms, updated_at_ms, object_id FROM blobs WHERE key = ?",
        )
        .get(key) as BlobRow | null
    ) ?? null;
  }

  async put(key: string, data: BlobPutData, options?: BlobPutOptions): Promise<void> {
    const normalizedKey = validateKey(key);
    const bytes = await blobDataToBytes(data);
    const objectId = objectIdForKey(normalizedKey);
    const finalPath = this.objectPathForId(objectId);
    mkdirSync(dirname(finalPath), { recursive: true });
    const tmpPath = `${finalPath}.tmp`;
    await Bun.write(tmpPath, bytes);
    const { renameSync, statSync } = await import("node:fs");
    renameSync(tmpPath, finalPath);
    const size = statSync(finalPath).size;
    const nowMs = Date.now();
    const existing = this.row(normalizedKey);
    this.index
      .query(
        `INSERT INTO blobs (key, content_type, size_bytes, etag, visibility, created_at_ms, updated_at_ms, object_id)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(key) DO UPDATE SET
           content_type = excluded.content_type,
           size_bytes = excluded.size_bytes,
           etag = excluded.etag,
           visibility = excluded.visibility,
           updated_at_ms = excluded.updated_at_ms,
           object_id = excluded.object_id`,
      )
      .run(
        normalizedKey,
        options?.contentType?.trim() || DEFAULT_CONTENT_TYPE,
        size,
        etagForBytes(bytes),
        options?.public ? "public" : "private",
        existing?.created_at_ms ?? nowMs,
        nowMs,
        objectId,
      );
  }

  async getUrl(key: string): Promise<string> {
    const normalizedKey = validateKey(key);
    if (!this.row(normalizedKey)) {
      throw new Error(`ctx.blobs key not found: ${normalizedKey}`);
    }
    return `./blobs/${encodeBlobKeyToken(normalizedKey)}`;
  }

  async delete(key: string): Promise<void> {
    const normalizedKey = validateKey(key);
    const existing = this.row(normalizedKey);
    this.index.query("DELETE FROM blobs WHERE key = ?").run(normalizedKey);
    if (existing) {
      const { rmSync } = await import("node:fs");
      try {
        rmSync(this.objectPathForId(existing.object_id), { force: true });
      } catch {
        /* best-effort */
      }
    }
  }

  async head(key: string): Promise<BlobMetadata | null> {
    const row = this.row(validateKey(key));
    return row ? metadataFromRow(row) : null;
  }

  async list(prefix = ""): Promise<BlobMetadata[]> {
    const normalizedPrefix = validatePrefix(prefix);
    const rows = this.index
      .query(
        "SELECT key, content_type, size_bytes, etag, visibility, created_at_ms, updated_at_ms, object_id FROM blobs WHERE key LIKE ? ORDER BY key ASC",
      )
      .all(`${normalizedPrefix}%`) as BlobRow[];
    return rows.map(metadataFromRow);
  }

  /** Serve helper for GET /blobs/<token>: returns file + content type. */
  async fileForToken(token: string): Promise<{ file: ReturnType<typeof Bun.file>; contentType: string } | null> {
    const key = decodeBlobKeyToken(token);
    if (!key) return null;
    const row = this.row(key);
    if (!row) return null;
    const file = Bun.file(this.objectPathForId(row.object_id));
    if (!(await file.exists())) return null;
    return { file, contentType: row.content_type };
  }
}
