import logging

# Fix for SDK structure - try/except not needed if we assume HAVE_LIGHTER_SDK is checked before usage
# but to be safe we import inside try/except block if this file is imported at top level
try:
    from lighter.signer_client import SignerClient
except ImportError:
    # If SDK not present, we can't inherit.
    # We define a dummy or let it fail when accessed.
    SignerClient = object

logger = logging.getLogger(__name__)


class SaferSignerClient(SignerClient):
    """
    Subclass of SignerClient that fixes the switch_api_key crash.
    Original SDK tries to .decode() an int return value.
    """

    def switch_api_key(self, api_key_index: int):
        try:
            return super().switch_api_key(int(api_key_index))
        except AttributeError as ae:
            if "'int' object has no attribute 'decode'" in str(ae):
                # This means success! The int was returned (e.g. transaction receipt or 0)
                # FIX: Return None instead of string, otherwise SDK treats it as an error message!
                return None
            raise ae
        except Exception as e:
            logger.warning(f"⚠️ SaferSignerClient.switch_api_key warning: {e}")
            return None
