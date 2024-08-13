import datetime
from contextlib import contextmanager
from functools import wraps
from collections import defaultdict
from getpass import getpass
from pathlib import Path
from typing import Callable, Dict, Optional

import click
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from tuf.repository_tool import import_rsakey_from_pem
from ykman.device import list_all_devices
from yubikit.core.smartcard import SmartCardConnection
from ykman.piv import (
    KEY_TYPE,
    MANAGEMENT_KEY_TYPE,
    SLOT,
    PivSession,
    generate_random_management_key,
)
from yubikit.piv import (
    DEFAULT_MANAGEMENT_KEY,
    PIN_POLICY,
    InvalidPinError,
)

from taf.constants import DEFAULT_RSA_SIGNATURE_SCHEME
from taf.exceptions import InvalidPINError, YubikeyError
from taf.utils import get_pin_for

DEFAULT_PIN = "123456"
DEFAULT_PUK = "12345678"
EXPIRATION_INTERVAL = 36500

_yks_data_dict: Dict = defaultdict(dict)


def add_key_id_mapping(serial_num: str, keyid: str) -> None:
    if "ids" not in _yks_data_dict:
        _yks_data_dict["ids"] = defaultdict(dict)
    _yks_data_dict["ids"][keyid] = serial_num


def add_key_pin(serial_num: str, pin: str) -> None:
    _yks_data_dict[serial_num]["pin"] = pin


def add_key_public_key(serial_num: str, public_key: Dict) -> None:
    _yks_data_dict[serial_num]["public_key"] = public_key


def get_key_pin(serial_num: int) -> Optional[str]:
    if serial_num in _yks_data_dict:
        return _yks_data_dict.get(serial_num, {}).get("pin")
    return None


def get_key_serial_by_id(keyid: str) -> Optional[str]:
    return _yks_data_dict.get("ids", {}).get(keyid)


def get_key_public_key(serial_num: str) -> Optional[Dict]:
    if serial_num in _yks_data_dict:
        return _yks_data_dict.get(serial_num, {}).get("public_key")
    return None


def raise_yubikey_err(msg: Optional[str] = None) -> Callable:
    """Decorator used to catch all errors raised by yubikey-manager and raise
    YubikeyError. We don't need to handle specific cases.
    """

    def wrapper(f):
        @wraps(f)
        def decorator(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except YubikeyError:
                raise
            except Exception as e:
                err_msg = (
                    f"{msg} Reason: ({type(e).__name__}) {str(e)}" if msg else str(e)
                )
                raise YubikeyError(err_msg) from e

        return decorator

    return wrapper


@contextmanager
def _yk_piv_ctrl(serial=None, pub_key_pem=None):
    """Context manager to open connection and instantiate Piv Session.

    Args:
        - serial (optional): Specify the serial number of the YubiKey to use.
        - pub_key_pem (optional): Match Yubikey's public key (PEM) if multiple keys
                                  are inserted.

    Returns:
        - ykman.piv.PivSession

    Raises:
        - YubikeyError
    """
    if pub_key_pem is not None:
        for dev, info in list_all_devices():
            with dev.open_connection(SmartCardConnection) as connection:
                session = PivSession(connection)
                device_pub_key_pem = (
                    session.get_certificate(SLOT.SIGNATURE)
                    .public_key()
                    .public_bytes(
                        encoding=serialization.Encoding.PEM,
                        format=serialization.PublicFormat.SubjectPublicKeyInfo,
                    )
                    .decode("utf-8")
                )
                if (
                    device_pub_key_pem == pub_key_pem
                    or device_pub_key_pem[:-1] == pub_key_pem
                ):
                    yield session, info.serial
                    break
    else:
        for dev, info in list_all_devices():
            if serial is None or info.serial == serial:
                with dev.open_connection(SmartCardConnection) as connection:
                    session = PivSession(connection)
                    yield session, info.serial
                    break


def is_inserted():
    """Checks if YubiKey is inserted.

    Args:
        None

    Returns:
        True if at least one Yubikey is inserted (bool)

    Raises:
        - YubikeyError
    """
    return len(list(list_all_devices())) > 0


@raise_yubikey_err()
def is_valid_pin(pin, serial=None):
    """Checks if given pin is valid.

    Args:
        pin (str): Yubikey PIV PIN.
        serial (optional): Specify the serial number of the YubiKey to use.

    Returns:
        tuple: True if PIN is valid, otherwise False, number of PIN retries.

    Raises:
        - YubikeyError
    """
    with _yk_piv_ctrl(serial=serial) as (ctrl, _):
        try:
            ctrl.verify_pin(pin)
            return True, None
        except InvalidPinError:
            return False, ctrl.get_pin_attempts()


@raise_yubikey_err("Cannot get serial number.")
def get_serial_num(pub_key_pem=None, serial=None):
    """Get Yubikey serial number.

    Args:
        - pub_key_pem (optional): Match Yubikey's public key (PEM) if multiple keys
                                  are inserted.
        - serial (optional): Specify the serial number of the YubiKey to use.

    Returns:
        Yubikey serial number.

    Raises:
        - YubikeyError
    """
    with _yk_piv_ctrl(pub_key_pem=pub_key_pem, serial=serial) as (_, serial_num):
        return serial_num


@raise_yubikey_err("Cannot export x509 certificate.")
def export_piv_x509(
    cert_format=serialization.Encoding.PEM, pub_key_pem=None, serial=None
):
    """Exports YubiKey's PIV slot x509.

    Args:
        - cert_format (str): One of 'serialization.Encoding' formats.
        - pub_key_pem (optional): Match Yubikey's public key (PEM) if multiple keys
                                  are inserted.
        - serial (optional): Specify the serial number of the YubiKey to use.

    Returns:
        PIV x509 certificate in a given format (bytes).

    Raises:
        - YubikeyError
    """
    with _yk_piv_ctrl(pub_key_pem=pub_key_pem, serial=serial) as (ctrl, _):
        x509 = ctrl.get_certificate(SLOT.SIGNATURE)
        return x509.public_bytes(encoding=cert_format)


@raise_yubikey_err("Cannot export public key.")
def export_piv_pub_key(
    pub_key_format=serialization.Encoding.PEM, pub_key_pem=None, serial=None
):
    """Exports YubiKey's PIV slot public key.

    Args:
        - pub_key_format (str): One of 'serialization.Encoding' formats.
        - pub_key_pem (optional): Match Yubikey's public key (PEM) if multiple keys
                                  are inserted.
        - serial (optional): Specify the serial number of the YubiKey to use.

    Returns:
        PIV public key in a given format (bytes).

    Raises:
        - YubikeyError
    """
    with _yk_piv_ctrl(pub_key_pem=pub_key_pem, serial=serial) as (ctrl, _):
        x509 = ctrl.get_certificate(SLOT.SIGNATURE)
        return x509.public_key().public_bytes(
            encoding=pub_key_format,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )


@raise_yubikey_err("Cannot export yk certificate.")
def export_yk_certificate(certs_dir, key, serial=None):
    if certs_dir is None:
        certs_dir = Path.home()
    else:
        certs_dir = Path(certs_dir)
    certs_dir.mkdir(parents=True, exist_ok=True)
    cert_path = certs_dir / f"{key['keyid']}.cert"
    print(f"Exporting certificate to {cert_path}")

    # Use the serial parameter to ensure we are interacting with the correct YubiKey
    with _yk_piv_ctrl(serial=serial) as (ctrl, _):
        with open(cert_path, "wb") as f:
            f.write(
                ctrl.get_certificate(SLOT.SIGNATURE).public_bytes(
                    encoding=serialization.Encoding.PEM
                )
            )


@raise_yubikey_err("Cannot get public key in TUF format.")
def get_piv_public_key_tuf(
    scheme=DEFAULT_RSA_SIGNATURE_SCHEME, pub_key_pem=None, serial=None
):
    """Return public key from a Yubikey in TUF's RSAKEY_SCHEMA format.

    Args:
        - scheme (str): RSA signature scheme (default is rsa-pkcs1v15-sha256)
        - pub_key_pem (optional): Match Yubikey's public key (PEM) if multiple keys
                                  are inserted.
        - serial (optional): Specify the serial number of the YubiKey to use.

    Returns:
        A dictionary containing the RSA keys and other identifying information
        from inserted smart card.
        Conforms to 'securesystemslib.formats.RSAKEY_SCHEMA'.

    Raises:
        - YubikeyError
    """
    pub_key_pem = export_piv_pub_key(pub_key_pem=pub_key_pem, serial=serial).decode(
        "utf-8"
    )
    return import_rsakey_from_pem(pub_key_pem, scheme)


@raise_yubikey_err("Cannot sign data.")
def sign_piv_rsa_pkcs1v15(data, pin, pub_key_pem=None, serial=None):
    """Sign data with key from YubiKey's PIV slot.

    Args:
        - data (bytes): Data to be signed
        - pin (str): Pin for PIV slot login.
        - pub_key_pem (optional): Match Yubikey's public key (PEM) if multiple keys
                                  are inserted.
        - serial (optional): Specify the serial number of the YubiKey to use.

    Returns:
        Signature (bytes)

    Raises:
        - YubikeyError
    """
    with _yk_piv_ctrl(pub_key_pem=pub_key_pem, serial=serial) as (ctrl, _):
        ctrl.verify_pin(pin)
        return ctrl.sign(
            SLOT.SIGNATURE, KEY_TYPE.RSA2048, data, hashes.SHA256(), padding.PKCS1v15()
        )


@raise_yubikey_err("Cannot setup Yubikey.")
def setup(
    pin,
    cert_cn,
    cert_exp_days=365,
    pin_retries=10,
    private_key_pem=None,
    mgm_key=generate_random_management_key(MANAGEMENT_KEY_TYPE.TDES),
    serial=None,
):
    """Use to setup inserted Yubikey, with following steps (order is important):
      - reset to factory settings
      - set management key
      - generate key(RSA2048) or import given one
      - generate and import self-signed certificate (X509)
      - set pin retries
      - set pin
      - set puk (same as pin)

    Args:
        - cert_cn (str): x509 common name
        - cert_exp_days (int): x509 expiration (in days from now)
        - pin_retries (int): Number of retries for PIN
        - private_key_pem (optional): Private key in PEM format. If given, it will be
                                      imported to Yubikey.
        - mgm_key (bytes): New management key
        - serial (optional): Specify the serial number of the YubiKey to use.

    Returns:
        PIV public key in PEM format (bytes)

    Raises:
        - YubikeyError
    """
    with _yk_piv_ctrl(serial=serial) as (ctrl, _):
        # Factory reset and set PINs
        ctrl.reset()

        ctrl.authenticate(MANAGEMENT_KEY_TYPE.TDES, DEFAULT_MANAGEMENT_KEY)
        ctrl.set_management_key(MANAGEMENT_KEY_TYPE.TDES, mgm_key)

        # Determine the first available slot
        available_slot = None
        for slot, slot_enum in [
            ("SIGNATURE", SLOT.SIGNATURE),
            ("AUTHENTICATION", SLOT.AUTHENTICATION),
            ("KEY_MANAGEMENT", SLOT.KEY_MANAGEMENT),
            ("CARD_AUTH", SLOT.CARD_AUTH),
        ]:
            try:
                ctrl.get_certificate(slot_enum)
                print(f"Slot {slot} already has a key.")
            except Exception:
                available_slot = slot_enum
                print(f"Slot {slot} is available and will be used.")
                break

        if available_slot is None:
            raise YubikeyError("No available slots found on the YubiKey.")

        # Generate RSA2048
        if private_key_pem is None:
            private_key = rsa.generate_private_key(65537, 2048, default_backend())
            pub_key = private_key.public_key()
        else:
            try:
                private_key = load_pem_private_key(
                    private_key_pem, None, default_backend()
                )
            except TypeError:
                pem_pwd = getpass("Enter pem file password:\n")
                if pem_pwd:
                    pem_pwd = pem_pwd.encode()
                private_key = load_pem_private_key(
                    private_key_pem, pem_pwd, default_backend()
                )

        ctrl.put_key(available_slot, private_key, PIN_POLICY.ALWAYS)
        pub_key = private_key.public_key()
        ctrl.authenticate(MANAGEMENT_KEY_TYPE.TDES, mgm_key)
        ctrl.verify_pin(DEFAULT_PIN)

        now = datetime.datetime.now()
        valid_to = now + datetime.timedelta(days=cert_exp_days)

        name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, cert_cn)])
        # Generate and import certificate
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(pub_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(valid_to)
            .sign(private_key, hashes.SHA256(), default_backend())
        )

        ctrl.put_certificate(available_slot, cert)

        ctrl.set_pin_attempts(pin_attempts=pin_retries, puk_attempts=pin_retries)
        ctrl.change_pin(DEFAULT_PIN, pin)
        ctrl.change_puk(DEFAULT_PUK, pin)

    return pub_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def setup_new_yubikey(serial_num, scheme=DEFAULT_RSA_SIGNATURE_SCHEME):
    pin = get_key_pin(serial_num)
    cert_cn = input("Enter key holder's name: ")
    print("Generating key, please wait...")
    pub_key_pem = setup(
        pin, cert_cn, cert_exp_days=EXPIRATION_INTERVAL, serial=serial_num
    ).decode("utf-8")
    scheme = DEFAULT_RSA_SIGNATURE_SCHEME
    key = import_rsakey_from_pem(pub_key_pem, scheme)
    return key


def get_and_validate_pin(key_name, serial=None, pin_confirm=True, pin_repeat=True):
    valid_pin = False
    while not valid_pin:
        pin = get_pin_for(key_name, pin_confirm, pin_repeat)
        valid_pin, retries = is_valid_pin(pin, serial=serial)
        if not valid_pin and not retries:
            raise InvalidPINError("No retries left. YubiKey locked.")
        if not valid_pin:
            if not click.confirm(
                f"Incorrect PIN. Do you want to try again? {retries} retries left."
            ):
                raise InvalidPINError("PIN input cancelled")
    return pin


@raise_yubikey_err("Cannot get serial numbers.")
def get_all_serials():
    """Get serial numbers of all connected YubiKeys."""
    serial_numbers = []
    yubikeys = list_all_devices()  # Function that lists all connected YubiKeys
    if not yubikeys:
        print("No YubiKeys connected.")
    else:
        for _, info in yubikeys:
            try:
                serial_numbers.append(info.serial)
            except AttributeError:
                print(f"Failed to get serial for YubiKey: {info}")
                continue
    return serial_numbers


def check_yubikey_serial(
    serial_num,
    role,
    taf_repo,
    creating_new_key,
    loaded_yubikeys,
    hide_already_loaded_message,
):
    if (
        loaded_yubikeys is not None
        and serial_num in loaded_yubikeys
        and role in loaded_yubikeys[serial_num]
    ):
        if not hide_already_loaded_message:
            print("Key already loaded")
        return False, None
    return True, None


def handle_yubikey_pin(serial_num, creating_new_key, key_name, pin_confirm, pin_repeat):
    if get_key_pin(serial_num) is None:
        if creating_new_key:
            pin = get_pin_for(key_name, pin_confirm, pin_repeat)
        else:
            pin = get_and_validate_pin(
                key_name,
                serial=serial_num,
                pin_confirm=pin_confirm,
                pin_repeat=pin_repeat,
            )
        add_key_pin(serial_num, pin)


def yubikey_prompt(
    key_name,
    role=None,
    taf_repo=None,
    registering_new_key=False,
    creating_new_key=False,
    loaded_yubikeys=None,
    pin_confirm=True,
    pin_repeat=True,
    prompt_message=None,
    retry_on_failure=True,
    hide_already_loaded_message=False,
    serial=None,
):
    def _read_and_check_yubikey(
        key_name,
        role,
        taf_repo,
        registering_new_key,
        creating_new_key,
        loaded_yubikeys,
        pin_confirm,
        pin_repeat,
        prompt_message,
        retrying,
        serial,
    ):
        if retrying:
            if prompt_message is None:
                prompt_message = f"Please insert {key_name} YubiKey and press ENTER"
            getpass(prompt_message)

        serial_nums_to_check = [serial] if serial else get_all_serials()

        for serial_num in serial_nums_to_check:
            try:
                if serial_num is None:
                    print("YubiKey not inserted")
                    return False, None, None

                success, public_key = check_yubikey_serial(
                    serial_num,
                    role,
                    taf_repo,
                    creating_new_key,
                    loaded_yubikeys,
                    hide_already_loaded_message,
                )
                if not success:
                    continue

                public_key = (
                    get_piv_public_key_tuf(serial=serial_num)
                    if not creating_new_key
                    else None
                )

                if (
                    not registering_new_key
                    and role
                    and taf_repo
                    and not taf_repo.is_valid_metadata_yubikey(role, public_key)
                ):
                    print(f"The inserted YubiKey is not a valid {role} key")
                    continue

                handle_yubikey_pin(
                    serial_num, creating_new_key, key_name, pin_confirm, pin_repeat
                )

                if get_key_public_key(serial_num) is None and public_key is not None:
                    add_key_public_key(serial_num, public_key)
                    add_key_id_mapping(serial_num, key_name)

                if role:
                    loaded_yubikeys = loaded_yubikeys or {}
                    loaded_yubikeys.setdefault(serial_num, []).append(role)

                return True, public_key, serial_num

            except Exception as e:
                print(f"Error checking YubiKey with serial {serial_num}: {e}")
                continue

        return False, None, None

    retry_counter = 0
    while True:
        success, key, serial_num = _read_and_check_yubikey(
            key_name,
            role,
            taf_repo,
            registering_new_key,
            creating_new_key,
            loaded_yubikeys,
            pin_confirm,
            pin_repeat,
            prompt_message,
            retrying=retry_counter > 0,
            serial=serial,
        )
        if not success and not retry_on_failure:
            return None, None
        if success:
            return key, serial_num
        retry_counter += 1


def list_connected_yubikeys():
    """Lists all connected YubiKeys with their serial numbers and details."""
    yubikeys = list_all_devices()
    if not yubikeys:
        print("No YubiKeys connected.")
    else:
        for index, (_, info) in enumerate(yubikeys, start=1):
            print(f"YubiKey {index}:")
            print(f"  Serial Number: {info.serial}")
            print(f"  Version: {info.version}")
            print(f"  Form Factor: {info.form_factor}")
