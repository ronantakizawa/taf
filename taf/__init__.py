"""Add platform-dependent libraries to the path.
"""
import sys


class YubikeyMissingLibrary:
    """If `yubikey-manager` is not installed and we try to use any function from `taf.yubikey`
    module, we will log appropriate error message and exit with code 1.
    """

    ERR_MSG = '"yubikey-manager" is not installed. Run "pip install taf[yubikey]" to install it.'

    def __getattr__(self, name):
        from taf.log import taf_logger

        taf_logger.warning(YubikeyMissingLibrary.ERR_MSG)
        sys.exit(1)
