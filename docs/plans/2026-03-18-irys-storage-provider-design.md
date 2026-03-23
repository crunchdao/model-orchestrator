# Irys Storage Provider for Model Uploads

## Goal

Add Irys (permanent decentralized storage on Arweave) as a configurable storage provider alongside S3 for model uploads. This enables immutable, tamper-proof model storage with decentralized access — models can be retrieved without depending on AWS.

## Requirements

- Irys exists alongside S3 as a per-project configurable storage provider
- Frontend uploads directly to Irys — model data never passes through the backend
- Backend orchestrates upload lifecycle: wallet signing (gasless pattern), funding, and metadata storage
- Phala CVM downloads from Irys gateway via an abstracted, configurable storage interface
- Client-side ECIES encryption precedes upload (unchanged from current flow)

## Systems Affected

1. **competition-service** (tournament repo) — upload orchestration, storage metadata
2. **tournament-webapp** — frontend upload strategy
3. **model-orchestrator** — resolves storage references, passes to CVM
4. **cvm-phala-cruncher** — downloads models from Irys or S3

## Design

### 1. Backend (competition-service)

**Provider enum**: Add `IRYS` to `Upload.Provider`.

**Project-level configuration**: New field on Competition/Project to select the storage provider (`S3` or `IRYS`).

**New entity fields**:
- `ModelFile.storageReference` (nullable string) — for Irys, stores the transaction ID. For S3, remains null (path derived at runtime from `models/{modelId}/{filename}`).

**Gasless upload endpoints** (new):
- `GET /v1/uploads/irys/public-key` — returns the server's Irys signing public key
- `POST /v1/uploads/irys/sign` — signs a transaction hash with the server's private key (only the hash, never the data)
- `POST /v1/uploads/irys/fund` — lazy-funds the Irys node for the given upload size

**IrysSigningService**: New service holding the private key (from env config). Exposes `getPublicKey()` and `sign(hash)`.

**Upload flow changes for Irys**:
- `UploadService.create()` sets `provider = IRYS`
- `getChunkRequest()` is not used for Irys — frontend uses the gasless signing endpoints instead
- On `complete()`, frontend reports Irys transaction IDs. Backend stores them on the ModelFile entities.
- `ModelStorageService` skips the S3 copy step for Irys — data is already at its final location. Records the transaction ID references only.

**New API endpoint**:
- `GET /v4/submissions/{submissionId}/storage` — returns provider type and file-to-reference mapping for the model-orchestrator to consume:
  ```json
  {
    "provider": "IRYS",
    "files": {
      "main.py": "irys-tx-id-abc123",
      "requirements.txt": "irys-tx-id-def456"
    }
  }
  ```

### 2. Frontend (tournament-webapp)

**Upload strategy pattern**: Replace the hardcoded S3 flow in `uploadFile.ts` with a strategy selection based on `upload.provider`:

- **S3Strategy** — existing flow: request presigned URL, PUT to S3, confirm ETag
- **IrysStrategy** — new: get server public key, create WebIrys with gasless provider (signing delegated to backend via `/v1/uploads/irys/sign`), call `irys.uploadFile()`, report transaction ID back to backend

**Strategy selection**: `POST /v1/uploads` response includes `provider: "AWS_S3" | "IRYS"`. The `uploadFile()` function picks the right strategy.

**Encryption**: Unchanged — `EncryptedFileReader` runs before the upload strategy. The strategy receives encrypted bytes.

**Dependencies**: Add `@irys/sdk` (`WebIrys`) to the webapp.

**Chunk handling**: Irys SDK manages chunking internally. Backend's chunk tracking simplifies to a single chunk for Irys uploads.

**Completion**: After `irys.uploadFile()` returns, frontend calls `completeUpload(uploadId)` with the Irys transaction ID(s).

### 3. Model Orchestrator

**TournamentApi extension**: Add a method to resolve storage references:
- Calls `GET /v4/submissions/{submissionId}/storage` on the competition-service
- Returns provider type + file→reference mapping

**SpawnteeClient.build_model() payload expansion**:
```json
{
  "submission_id": "abc-123",
  "storage_provider": "IRYS",
  "storage_references": {
    "main.py": "irys-tx-id-abc123",
    "requirements.txt": "irys-tx-id-def456"
  }
}
```

For S3, `storage_provider` is `"S3"` and `storage_references` can be omitted (cruncher derives paths from submission_id as today, maintaining backward compatibility).

### 4. Phala CVM (cvm-phala-cruncher)

**StorageClient abstraction** in `registry.py`:

```python
class StorageClient(ABC):
    async def download_file(self, reference: str) -> bytes: ...

class S3StorageClient(StorageClient):
    # Existing boto3 logic

class IrysStorageClient(StorageClient):
    # HTTP GET to gateway_url/{transaction_id} using httpx
```

**Build handler changes**: Reads `storage_provider` and `storage_references` from the `POST /build-model` request. Instantiates the correct client.

**Backward compatibility**: If `storage_provider` is absent in the request, defaults to S3 with the existing path derivation from `submission_id`.

**Gateway configuration**: Irys gateway URL (`https://gateway.irys.xyz/`) configurable via environment variable.

**Encryption/decryption**: Unchanged — download bytes from whichever source, then decrypt with TEE-derived key.

## Data Flow

```
Frontend → encrypt → irys.uploadFile() → Irys Network (permanent storage)
        → POST /complete (tx IDs) → competition-service (stores references)

model-orchestrator → GET /v4/submissions/{id}/storage → competition-service
                   → POST /build-model (with provider + references) → Phala CVM

Phala CVM → GET gateway.irys.xyz/{txId} → Irys Network
          → decrypt → build Docker image
```

## Irys Gasless Upload Pattern (Reference)

Based on https://github.com/Irys-xyz/gasless-uploader:

1. Client requests server's public key
2. Client sends file size to server for lazy funding (server funds Irys node if balance insufficient)
3. Client creates upload transaction, server signs the transaction hash (not the data)
4. Client constructs WebIrys with custom provider that delegates signing to server
5. Client calls `irys.uploadFile()` — data goes directly from browser to Irys
6. Returns `tx.id` — retrievable at `https://gateway.irys.xyz/{txId}`

## Key Decisions

- **Irys alongside S3**, not replacing it — configurable per project
- **Gasless pattern** — backend owns wallet/funding/signing, data never touches backend
- **Client-side encryption before upload** — same ECIES flow as today
- **Transaction IDs persisted on ModelFile** — new `storageReference` field
- **Orchestrator resolves references** via tournament API, passes to CVM
- **CVM storage client abstraction** — pluggable S3/Irys implementations
