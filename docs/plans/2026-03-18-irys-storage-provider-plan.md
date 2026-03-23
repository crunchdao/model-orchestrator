# Irys Storage Provider Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add Irys as a configurable storage provider alongside S3 for model uploads, with changes across competition-service, tournament-webapp, model-orchestrator, and cvm-phala-cruncher.

**Architecture:** Frontend uploads encrypted model files directly to Irys using the gasless pattern (backend signs, frontend sends data). Backend stores Irys transaction IDs on ModelFile entities. Model-orchestrator resolves storage references via the tournament API and passes them to the Phala CVM, which downloads from the Irys gateway.

**Tech Stack:** Java/Spring Boot (competition-service), TypeScript/Next.js (webapp), Python/FastAPI (orchestrator + cruncher), Irys SDK (`@irys/sdk`), `httpx` (Python HTTP client)

**Design doc:** `docs/plans/2026-03-18-irys-storage-provider-design.md`

---

## Task 1: Database Migration — Add `storage_reference` to `model_files`

**Repo:** `tournament/competition-service`

**Files:**
- Create: `src/main/resources/db/migration/2026/03/V000325__model_file_add_storage_reference_column.sql`

**Step 1: Write the migration**

```sql
ALTER TABLE model_files ADD COLUMN storage_reference VARCHAR(255) NULL;
```

**Step 2: Commit**

```bash
git add src/main/resources/db/migration/2026/03/V000325__model_file_add_storage_reference_column.sql
git commit -m "feat: add storage_reference column to model_files"
```

---

## Task 2: Add `IRYS` to `Upload.Provider` Enum

**Repo:** `tournament/competition-service`

**Files:**
- Modify: `src/main/java/com/crunchdao/tournament/competition/domain/upload/Upload.java:239-243`

**Step 1: Add IRYS to the Provider enum**

At line 239-243, the current enum:
```java
public enum Provider {
    AWS_S3,
}
```

Change to:
```java
public enum Provider {
    AWS_S3,
    IRYS,
}
```

**Step 2: Update the frontend UploadProvider enum**

In `tournament-webapp/apps/web/src/modules/upload/domain/types.ts:1-3`:

```typescript
export enum UploadProvider {
  AWS_S3 = "AWS_S3",
  IRYS = "IRYS",
}
```

**Step 3: Commit**

```bash
git commit -m "feat: add IRYS to Upload.Provider enum (backend + frontend)"
```

---

## Task 3: Add `storageReference` Field to `ModelFile` Entity

**Repo:** `tournament/competition-service`

**Files:**
- Modify: `src/main/java/com/crunchdao/tournament/competition/domain/model/ModelFile.java:46-56`

**Step 1: Add the field**

After the existing `mimeType` field (line 56), add:

```java
@Column
private String storageReference;
```

The `@Data` + `@Accessors(chain = true)` Lombok annotations will auto-generate the getter/setter.

**Step 2: Commit**

```bash
git commit -m "feat: add storageReference field to ModelFile entity"
```

---

## Task 4: Create `IrysConfigurationProperties`

**Repo:** `tournament/competition-service`

**Files:**
- Create: `src/main/java/com/crunchdao/tournament/competition/configuration/properties/IrysConfigurationProperties.java`

**Step 1: Write the properties class**

```java
package com.crunchdao.tournament.competition.configuration.properties;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;
import org.springframework.validation.annotation.Validated;

import lombok.Data;

@Data
@Validated
@Component
@ConfigurationProperties(prefix = "app.irys")
public class IrysConfigurationProperties {

    private boolean enabled = false;
    private String privateKey;
    private String network = "mainnet";
    private String token = "ethereum";
    private String rpcUrl;
}
```

**Step 2: Commit**

```bash
git commit -m "feat: add IrysConfigurationProperties"
```

---

## Task 5: Create `IrysSigningService`

**Repo:** `tournament/competition-service`

**Files:**
- Create: `src/main/java/com/crunchdao/tournament/competition/domain/irys/IrysSigningService.java`

**Step 1: Write the signing service**

This service holds the server's private key and provides signing + public key retrieval for the gasless upload pattern. The exact signing logic depends on the Irys SDK's expected signature format (TypedEthereumSigner from `arbundles`). For EVM wallets, the signing uses EIP-712 typed data signing.

```java
package com.crunchdao.tournament.competition.domain.irys;

import java.math.BigInteger;

import org.bouncycastle.crypto.params.ECPrivateKeyParameters;
import org.bouncycastle.crypto.signers.ECDSASigner;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Service;

import com.crunchdao.tournament.competition.configuration.properties.IrysConfigurationProperties;

import lombok.extern.slf4j.Slf4j;

@Slf4j
@Service
@ConditionalOnProperty(prefix = "app.irys", name = "enabled", havingValue = "true")
public class IrysSigningService {

    private final IrysConfigurationProperties properties;
    private final byte[] privateKeyBytes;
    private final byte[] publicKeyBytes;

    public IrysSigningService(IrysConfigurationProperties properties) {
        this.properties = properties;
        this.privateKeyBytes = hexToBytes(properties.getPrivateKey());
        this.publicKeyBytes = derivePublicKey(this.privateKeyBytes);

        log.info("initialized - network={} token={}", properties.getNetwork(), properties.getToken());
    }

    public byte[] getPublicKey() {
        return publicKeyBytes.clone();
    }

    public byte[] sign(byte[] data) {
        // Sign using secp256k1 ECDSA — matches TypedEthereumSigner from arbundles
        // Implementation depends on exact Irys SDK signature format
        // This is a placeholder — exact implementation requires testing against Irys SDK
        throw new UnsupportedOperationException("TODO: implement signing matching Irys TypedEthereumSigner format");
    }

    public String getNetwork() {
        return properties.getNetwork();
    }

    public String getToken() {
        return properties.getToken();
    }

    private static byte[] hexToBytes(String hex) {
        if (hex.startsWith("0x")) hex = hex.substring(2);
        byte[] bytes = new byte[hex.length() / 2];
        for (int i = 0; i < bytes.length; i++) {
            bytes[i] = (byte) Integer.parseInt(hex.substring(2 * i, 2 * i + 2), 16);
        }
        return bytes;
    }

    private static byte[] derivePublicKey(byte[] privateKey) {
        // Derive secp256k1 public key from private key
        // This is a placeholder — needs BouncyCastle or web3j implementation
        throw new UnsupportedOperationException("TODO: implement secp256k1 public key derivation");
    }
}
```

> **Note:** The exact signing implementation needs to match what the Irys `WebIrys` SDK expects from a `TypedEthereumSigner`. This will require testing against the Irys devnet. The placeholder methods should be filled in during integration testing.

**Step 2: Commit**

```bash
git commit -m "feat: add IrysSigningService with gasless signing support"
```

---

## Task 6: Create Irys REST Controller

**Repo:** `tournament/competition-service`

**Files:**
- Create: `src/main/java/com/crunchdao/tournament/competition/web/domain/irys/IrysRestControllerV1.java`

**Step 1: Write the controller**

```java
package com.crunchdao.tournament.competition.web.domain.irys;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.crunchdao.tournament.competition.domain.irys.IrysSigningService;

import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import lombok.RequiredArgsConstructor;

@RestController
@RequestMapping("/v1/uploads/irys")
@RequiredArgsConstructor
@Tag(name = "Irys")
@ConditionalOnProperty(prefix = "app.irys", name = "enabled", havingValue = "true")
public class IrysRestControllerV1 {

    private final IrysSigningService irysSigningService;

    @GetMapping("public-key")
    @Operation(summary = "Get the server's Irys signing public key.")
    public PublicKeyResponse getPublicKey() {
        byte[] pubKey = irysSigningService.getPublicKey();
        return new PublicKeyResponse(bytesToHex(pubKey));
    }

    @PostMapping("sign")
    @Operation(summary = "Sign upload data with the server's private key.")
    public SignResponse signData(@RequestBody SignRequest request) {
        byte[] signatureData = hexToBytes(request.signatureData());
        byte[] signature = irysSigningService.sign(signatureData);
        return new SignResponse(bytesToHex(signature));
    }

    @PostMapping("fund")
    @Operation(summary = "Lazy-fund the Irys node for the given file size.")
    public FundResponse fund(@RequestBody FundRequest request) {
        // TODO: implement lazy funding via Irys SDK (server-side)
        // Check current balance, fund if insufficient for request.size
        return new FundResponse("funded");
    }

    public record PublicKeyResponse(String pubKey) {}
    public record SignRequest(String signatureData) {}
    public record SignResponse(String signature) {}
    public record FundRequest(long size) {}
    public record FundResponse(String status) {}

    private static String bytesToHex(byte[] bytes) {
        StringBuilder sb = new StringBuilder();
        for (byte b : bytes) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    private static byte[] hexToBytes(String hex) {
        if (hex.startsWith("0x")) hex = hex.substring(2);
        byte[] bytes = new byte[hex.length() / 2];
        for (int i = 0; i < bytes.length; i++) {
            bytes[i] = (byte) Integer.parseInt(hex.substring(2 * i, 2 * i + 2), 16);
        }
        return bytes;
    }
}
```

**Step 2: Commit**

```bash
git commit -m "feat: add Irys REST controller for gasless upload endpoints"
```

---

## Task 7: Update `ModelStorageService` to Handle Irys Provider

**Repo:** `tournament/competition-service`

**Files:**
- Modify: `src/main/java/com/crunchdao/tournament/competition/domain/model/ModelStorageService.java:37-65`

**Step 1: Modify the `uploadFile` method**

The current private `uploadFile` method (lines 47-65) always does an S3 copy via `uploadService.consume()`. For Irys, we skip the copy and just record the storage reference.

Replace lines 47-65:

```java
@SneakyThrows
private void uploadFile(Model model, String path, Upload upload) {
    final var name = getFileName(path);
    final var size = upload.getSize();
    final var encrypted = upload.isEncrypted();

    var modelFile = new ModelFile()
        .setName(name)
        .setSize(size)
        .setEncrypted(encrypted)
        .setMimeType(upload.getMediaType());

    if (upload.getProvider() == Upload.Provider.IRYS) {
        modelFile.setStorageReference(upload.getProviderTransactionId());
        log.info("recorded irys reference - model={} name='{}' txId='{}'", model.getId(), name, upload.getProviderTransactionId());
    } else {
        final var key = getKey(model, name);
        log.info("storing - model={} key='{}' size={} encrypted={}", model.getId(), key, size, encrypted);
        uploadService.consume(upload, key);
    }

    model.addFile(modelFile);
}
```

**Step 2: Update `getUris` and `getUri` to handle Irys references**

For Irys files, the URI is simply `https://gateway.irys.xyz/{storageReference}`. Update `getUris()` (line 68) and `getUri()` (line 85) to check for a storage reference:

```java
public Map<String, URI> getUris(Model model) {
    final var keyPrefix = getKeyPrefix(model);

    return model.getFiles()
        .stream()
        .collect(Collectors.toMap(
            ModelFile::getName,
            (file) -> getFileUri(file, keyPrefix)
        ));
}

public URI getUri(ModelFile modelFile) {
    final var keyPrefix = getKeyPrefix(modelFile.getModel());
    return getFileUri(modelFile, keyPrefix);
}

private URI getFileUri(ModelFile file, String keyPrefix) {
    if (file.getStorageReference() != null) {
        return URI.create("https://gateway.irys.xyz/" + file.getStorageReference());
    }

    final var name = FilenameUtils.normalize(file.getName(), true);
    final var key = BlobStore.joinAndNormalize(keyPrefix, name);
    return blobStore.generatePresignedUrl(ObjectIdentifier.of(key), URI_EXPIRATION);
}
```

**Step 3: Commit**

```bash
git commit -m "feat: update ModelStorageService to handle Irys provider"
```

---

## Task 8: Add `providerTransactionId` to `Upload` Entity

**Repo:** `tournament/competition-service`

**Files:**
- Modify: `src/main/java/com/crunchdao/tournament/competition/domain/upload/Upload.java`
- Create: `src/main/resources/db/migration/2026/03/V000326__upload_add_provider_transaction_id_column.sql`

**Step 1: Add the migration**

```sql
ALTER TABLE uploads ADD COLUMN provider_transaction_id VARCHAR(255) NULL;
```

**Step 2: Add the field to Upload entity**

After the existing `providerId` field (around line 89), add:

```java
@Column
private String providerTransactionId;
```

**Step 3: Commit**

```bash
git commit -m "feat: add providerTransactionId to Upload entity"
```

---

## Task 9: Update Upload Complete Flow to Accept Transaction IDs

**Repo:** `tournament/competition-service`

**Files:**
- Modify: `src/main/java/com/crunchdao/tournament/competition/web/domain/upload/UploadRestControllerV1.java` (complete endpoint, ~line 182)
- Modify: `src/main/java/com/crunchdao/tournament/competition/domain/upload/UploadService.java:200-220`

**Step 1: Update the complete endpoint to accept an optional transaction ID**

The `completeUpload` endpoint needs to accept an optional body with the Irys transaction ID. For Irys uploads, the frontend sends the transaction ID when completing.

Add a form/request body class if not already present:

```java
public record UploadCompleteForm(
    @Nullable String transactionId
) {}
```

Update the controller's complete method to pass the transaction ID to `UploadService.complete()`.

**Step 2: Update `UploadService.complete()` to store the transaction ID**

In `complete()` (line 200), before setting status to SUCCEEDED, if the provider is IRYS:

```java
if (upload.getProvider() == Upload.Provider.IRYS && transactionId != null) {
    upload.setProviderTransactionId(transactionId);
}
```

**Step 3: Commit**

```bash
git commit -m "feat: accept Irys transaction ID on upload complete"
```

---

## Task 10: Create Storage Reference API Endpoint

**Repo:** `tournament/competition-service`

**Files:**
- Modify: `src/main/java/com/crunchdao/tournament/competition/web/domain/submission/SubmissionRestControllerV4.java` (or create a new controller)

**Step 1: Add endpoint**

This endpoint returns the storage provider and file references for a submission's model, consumed by the model-orchestrator.

```java
@GetMapping("{submissionId}/storage")
@Operation(summary = "Get storage references for a submission's model files.")
public StorageReferencesDto getStorageReferences(@PathVariable long submissionId) {
    final var submission = submissionService.get(submissionId);
    final var model = submission.getModel();

    if (model == null) {
        throw new ResponseStatusException(HttpStatus.NOT_FOUND, "No model for submission");
    }

    final var provider = model.getFiles().stream()
        .anyMatch(f -> f.getStorageReference() != null) ? "IRYS" : "S3";

    final var files = model.getFiles().stream()
        .filter(f -> f.getStorageReference() != null)
        .collect(Collectors.toMap(
            ModelFile::getName,
            ModelFile::getStorageReference
        ));

    return new StorageReferencesDto(provider, files);
}

public record StorageReferencesDto(
    String provider,
    Map<String, String> files
) {}
```

**Step 2: Commit**

```bash
git commit -m "feat: add storage references endpoint for model-orchestrator"
```

---

## Task 11: Frontend — Add Irys Upload Strategy

**Repo:** `tournament-webapp`

**Files:**
- Create: `apps/web/src/modules/upload/application/utils/uploadFileIrys.ts`
- Create: `apps/web/src/modules/upload/infrastructure/irysEndpoints.ts`
- Create: `apps/web/src/modules/upload/infrastructure/irysService.ts`

**Step 1: Add Irys infrastructure endpoints**

```typescript
// irysEndpoints.ts
export const irysEndpoints = {
  publicKey: () => `/v1/uploads/irys/public-key`,
  sign: () => `/v1/uploads/irys/sign`,
  fund: () => `/v1/uploads/irys/fund`,
};
```

**Step 2: Add Irys infrastructure service**

```typescript
// irysService.ts
import apiClient from "@/utils/api/client";
import { irysEndpoints } from "./irysEndpoints";

export const getIrysPublicKey = async (): Promise<string> => {
  const response = await apiClient.get(irysEndpoints.publicKey());
  return response.data.pubKey;
};

export const signIrysData = async (signatureData: string): Promise<string> => {
  const response = await apiClient.post(irysEndpoints.sign(), { signatureData });
  return response.data.signature;
};

export const fundIrysUpload = async (size: number): Promise<void> => {
  await apiClient.post(irysEndpoints.fund(), { size });
};
```

**Step 3: Create the Irys upload function**

```typescript
// uploadFileIrys.ts
import { WebIrys } from "@irys/sdk";
import { getIrysPublicKey, signIrysData, fundIrysUpload } from "../../infrastructure/irysService";
import { completeUpload } from "../../infrastructure/service";
import { UploadProgress } from "../../domain/types";

export async function uploadFileIrys(
  uploadId: string,
  file: File | Blob,
  fileName: string,
  onProgress: (progress: UploadProgress) => void,
  path: string,
  isEncrypted: boolean
): Promise<string> {
  onProgress({ path, state: "new-chunk", isEncrypted });

  const pubKeyHex = await getIrysPublicKey();
  const pubKey = Buffer.from(pubKeyHex, "hex");

  await fundIrysUpload(file.size);

  const provider = {
    getPublicKey: async () => pubKey,
    getSigner: () => ({
      getAddress: () => pubKey.toString(),
      _signTypedData: async (
        _domain: never,
        _types: never,
        message: { address: string; "Transaction hash": Uint8Array },
      ) => {
        const convertedMsg = Buffer.from(message["Transaction hash"]).toString("hex");
        const signature = await signIrysData(convertedMsg);
        const bSig = Buffer.from(signature, "hex");
        const pad = Buffer.concat([Buffer.from([0]), bSig]).toString("hex");
        return pad;
      },
    }),
    _ready: () => {},
  };

  const network = process.env.NEXT_PUBLIC_IRYS_NETWORK || "devnet";
  const token = process.env.NEXT_PUBLIC_IRYS_TOKEN || "ethereum";
  const wallet = { name: "ethersv5", provider };
  const irys = new WebIrys({ network, token, wallet });
  await irys.ready();

  onProgress({ path, state: "chunk", number: 1, count: 1, bytesSent: 0, bytesRate: 0, isEncrypted });

  const tags = [{ name: "Content-Type", value: "application/octet-stream" }];
  const tx = await irys.uploadFile(file, { tags });

  onProgress({ path, state: "completing", isEncrypted });

  return tx.id;
}
```

**Step 4: Commit**

```bash
git commit -m "feat: add Irys upload strategy for frontend"
```

---

## Task 12: Frontend — Integrate Irys Strategy into `uploadFile.ts`

**Repo:** `tournament-webapp`

**Files:**
- Modify: `apps/web/src/modules/upload/application/utils/uploadFile.ts`
- Modify: `apps/web/src/modules/upload/infrastructure/service.ts`

**Step 1: Update `completeUpload` in service.ts to accept optional transaction ID**

In `apps/web/src/modules/upload/infrastructure/service.ts`, update the `completeUpload` function:

```typescript
export const completeUpload = async (
  uploadId: string,
  transactionId?: string
): Promise<Upload> => {
  const response = await apiClient.post(
    uploadEndpoints.completeUpload(uploadId),
    transactionId ? { transactionId } : undefined
  );
  return response.data;
};
```

**Step 2: Update `uploadFile.ts` to route based on provider**

In `apps/web/src/modules/upload/application/utils/uploadFile.ts`, after the `postUpload()` call (line 27), add provider routing:

```typescript
import { uploadFileIrys } from "./uploadFileIrys";
import { UploadProvider } from "../../domain/types";

// After: const upload = await postUpload({...});

if (upload.provider === UploadProvider.IRYS) {
  const fileData = encryptedReader
    ? new Blob([await encryptedReader.readAll()])
    : file;

  const txId = await uploadFileIrys(
    upload.id,
    fileData,
    file.name,
    onProgress,
    path,
    !!encryptedReader
  );

  await completeUpload(upload.id, txId);

  onProgress({ path, state: "completed", isEncrypted: !!encryptedReader });

  return {
    id: upload.id,
    ephemeralPublicKey: encryptedReader?.getEphemeralPublicKey(),
  };
}

// ... existing S3 flow continues for AWS_S3 provider
```

**Step 3: Add `@irys/sdk` dependency**

```bash
cd apps/web && pnpm add @irys/sdk
```

**Step 4: Commit**

```bash
git commit -m "feat: route upload to Irys or S3 based on provider"
```

---

## Task 13: Model Orchestrator — Add Storage Reference Resolution

**Repo:** `model-orchestrator`

**Files:**
- Modify: `model_orchestrator/infrastructure/tournament.py`

**Step 1: Add method to TournamentApi**

```python
def load_storage_references(self, submission_id: str) -> dict | None:
    url = f"{self.url}/v4/submissions/{submission_id}/storage"
    try:
        response = requests.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error("Failed to fetch storage references for %s: %s", submission_id, e)
        return None
```

**Step 2: Commit**

```bash
git commit -m "feat: add storage reference resolution to TournamentApi"
```

---

## Task 14: Model Orchestrator — Pass Storage References to Spawntee

**Repo:** `model-orchestrator`

**Files:**
- Modify: `model_orchestrator/infrastructure/phala/_client.py:222-232`
- Modify: `model_orchestrator/infrastructure/phala/_builder.py:58-98`

**Step 1: Update `SpawnteeClient.build_model()` to accept storage references**

In `_client.py`, update `build_model` (line 222):

```python
def build_model(
    self,
    submission_id: str,
    model_name: str | None = None,
    storage_provider: str | None = None,
    storage_references: dict[str, str] | None = None,
) -> dict:
    payload = {"submission_id": submission_id}
    if model_name:
        payload["model_name"] = model_name
    if storage_provider:
        payload["storage_provider"] = storage_provider
    if storage_references:
        payload["storage_references"] = storage_references
    response = self._request("POST", "/build-model", json=payload)
    return response.json()
```

**Step 2: Update `PhalaModelBuilder.build()` to resolve and pass storage references**

In `_builder.py`, the `build()` method (line 58) needs access to the TournamentApi. The builder's `__init__` takes a `PhalaCluster` — the tournament API reference should be passed in or accessible via the cluster/config.

Update the builder to accept and use a tournament API:

```python
class PhalaModelBuilder(Builder):
    def __init__(self, cluster: PhalaCluster, tournament_api=None):
        super().__init__()
        self._cluster = cluster
        self._tournament_api = tournament_api

    def build(self, model: ModelRun, crunch: Crunch) -> tuple[str, str, str]:
        submission_id = model.code_submission_id

        storage_provider = None
        storage_references = None
        if self._tournament_api:
            storage_info = self._tournament_api.load_storage_references(submission_id)
            if storage_info and storage_info.get("provider") == "IRYS":
                storage_provider = storage_info["provider"]
                storage_references = storage_info.get("files")

        # ... existing capacity check ...
        self._cluster.ensure_capacity()
        client = self._cluster.head_client()

        try:
            result = client.build_model(
                submission_id,
                model_name=model.name,
                storage_provider=storage_provider,
                storage_references=storage_references,
            )
        except SpawnteeClientError as e:
            logger.error("Failed to submit build for %s: %s", submission_id, e)
            raise

        # ... rest unchanged ...
```

**Step 3: Commit**

```bash
git commit -m "feat: pass storage references from orchestrator to spawntee"
```

---

## Task 15: Phala CVM — Create `StorageClient` Abstraction

**Repo:** `cvm-phala-cruncher`

**Files:**
- Create: `spawntee/src/storage_client.py`

**Step 1: Write the storage client module**

```python
from abc import ABC, abstractmethod
import os

import boto3
import httpx
import structlog

logger = structlog.get_logger()

IRYS_GATEWAY_URL = os.environ.get("IRYS_GATEWAY_URL", "https://gateway.irys.xyz")


class StorageClient(ABC):
    @abstractmethod
    async def download_file(self, reference: str) -> bytes:
        ...


class S3StorageClient(StorageClient):
    def __init__(self, bucket: str, region: str):
        self._bucket = bucket
        self._region = region
        self._client = boto3.client("s3", region_name=region)

    async def download_file(self, reference: str) -> bytes:
        logger.info("downloading from s3", bucket=self._bucket, key=reference)
        response = self._client.get_object(Bucket=self._bucket, Key=reference)
        return response["Body"].read()


class IrysStorageClient(StorageClient):
    def __init__(self, gateway_url: str = IRYS_GATEWAY_URL):
        self._gateway_url = gateway_url.rstrip("/")

    async def download_file(self, reference: str) -> bytes:
        url = f"{self._gateway_url}/{reference}"
        logger.info("downloading from irys", url=url)
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=300)
            response.raise_for_status()
            return response.content
```

**Step 2: Commit**

```bash
git commit -m "feat: add StorageClient abstraction with S3 and Irys implementations"
```

---

## Task 16: Phala CVM — Update `SpawnRequest` and Build Flow

**Repo:** `cvm-phala-cruncher`

**Files:**
- Modify: `spawntee/src/model_service.py:31-35`
- Modify: `spawntee/src/registry.py:163-214`

**Step 1: Expand SpawnRequest to accept storage references**

In `model_service.py` at line 31-35:

```python
class SpawnRequest(BaseModel):
    """Request model for decrypt and spawn operation."""
    submission_id: str
    model_name: str | None = None
    storage_provider: str | None = None
    storage_references: dict[str, str] | None = None
```

**Step 2: Add an Irys download path in registry.py**

Add a new method to `Registry` for downloading from Irys, alongside the existing `download_and_decrypt_from_s3`:

```python
async def download_and_decrypt_from_irys(
    self, submission_id: str, storage_references: dict[str, str]
) -> dict[str, bytes]:
    from storage_client import IrysStorageClient

    client = IrysStorageClient()
    decrypted_files: dict[str, bytes] = {}

    for filename, tx_id in storage_references.items():
        logger.info("downloading from irys", filename=filename, tx_id=tx_id)
        encrypted_data = await client.download_file(tx_id)
        decrypted_content = await self._decrypt_file(
            encrypted_data, submission_id, filename
        )
        decrypted_files[filename] = decrypted_content
        logger.info("decrypted %s (%d bytes)", filename, len(decrypted_content))

    return decrypted_files
```

**Step 3: Update `ModelService.build_model` to route based on provider**

In the build flow (wherever `download_and_decrypt_from_s3` is called), add routing:

```python
if request.storage_provider == "IRYS" and request.storage_references:
    decrypted_files = await self.registry.download_and_decrypt_from_irys(
        request.submission_id, request.storage_references
    )
else:
    decrypted_files = await self.registry.download_and_decrypt_from_s3(
        request.submission_id
    )
```

**Step 4: Commit**

```bash
git commit -m "feat: support Irys download in Phala CVM build flow"
```

---

## Task 17: Integration Testing — End-to-End Irys Upload on Devnet

This task is manual and spans all four repos. Use the Irys devnet to validate:

1. Configure competition-service with `app.irys.enabled=true` and a devnet private key
2. Create a project/competition with Irys as the storage provider
3. From the webapp, upload a small test file — verify it hits the Irys signing endpoints and uploads to devnet
4. Verify the transaction ID is stored on the ModelFile entity
5. Verify the storage references endpoint returns the correct data
6. Test the orchestrator's `load_storage_references()` call
7. Test the Phala CVM's `IrysStorageClient` download from devnet gateway
8. Verify the full encrypt → upload → download → decrypt flow

---

## Implementation Order

The tasks are ordered for safe incremental delivery:

| Phase | Tasks | Repo | Description |
|-------|-------|------|-------------|
| 1 - Schema | 1, 2, 3, 4, 8 | competition-service | DB migrations + entity changes |
| 2 - Backend API | 5, 6, 9, 10 | competition-service | Signing service + endpoints |
| 3 - Backend Logic | 7 | competition-service | ModelStorageService update |
| 4 - Frontend | 11, 12 | tournament-webapp | Irys upload strategy |
| 5 - Orchestrator | 13, 14 | model-orchestrator | Storage reference passing |
| 6 - Cruncher | 15, 16 | cvm-phala-cruncher | Storage client abstraction |
| 7 - Integration | 17 | all | End-to-end testing |

Each phase can be developed and tested independently. Phase 1-3 (backend) and Phase 6 (cruncher) have no cross-dependencies and can be parallelized.
