import binascii

import cryptography.exceptions
from cryptography.hazmat.primitives.asymmetric import ed25519
from model_orchestrator.entities.model_run import ModelRun
from model_orchestrator.utils.logging_utils import get_logger, init_logger


class SignatureVerifier:
    def __init__(self, public_key: str):
        public_key_bytes = binascii.unhexlify(public_key)
        self.public_key = ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)

    def verify_signature(self, cruncher_id: str, model_id: str, code_submission_id: str, resource_id: str, signature: str) -> bool:
        try:
            signature, timestamp = signature.split(":")
            get_logger().trace(f"Timestamp: {timestamp}, Signature: {signature}")
            model_data = f'{cruncher_id}:{model_id}:{code_submission_id}:{resource_id}:{timestamp}'
            get_logger().trace(f"Model data to verify: {model_data}")
            b_model_data = model_data.encode()
            b_signature = binascii.unhexlify(signature)
            self.public_key.verify(b_signature, b_model_data)
            return True
        except cryptography.exceptions.InvalidSignature:
            pass
        except Exception as e:
            get_logger().error(f"Error verifying signature: {e}")

        return False


if __name__ == "__main__":
    get_logger().debug("Starting model signature verification")
    public_key = "e18a27536919fa9717393522ced3c78476965a498bab7d94e5e07a7dce1c5cc8"
    signature_b64 = "487c231985d70d2ab39d53c4fe2f6460255fac200954b80f9e3d6f61aaa87cc3c1bcf2e6036b6178cc1c4e6c54ec1545ff6d5e1fc933054f4cca3e86c96e0906:1739556339"

    frank_model = ModelRun(None, '9647', 'frank', 'bird-game', 'JZCYgKdpiTnz1WfNsRqAnfCgPof1365aLCQ9UEKU8jt', '19100', 'null', ModelRun.HardwareType.CPU, ModelRun.DesiredStatus.RUNNING)

    verifier = SignatureVerifier(public_key)
    is_valid = verifier.verify_signature(frank_model.cruncher_id, frank_model.model_id, frank_model.code_submission_id, frank_model.resource_id, signature_b64)
    print(f"Is the signature valid? {is_valid}")
