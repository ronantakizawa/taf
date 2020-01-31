import datetime
from pathlib import Path

import pytest
import securesystemslib
import taf.exceptions
import tuf
from taf.tests import TEST_WITH_REAL_YK
from taf.utils import to_tuf_datetime_format
import taf.yubikey as yk
from taf.constants import DEFAULT_RSA_SIGNATURE_SCHEME


@pytest.mark.skipif(TEST_WITH_REAL_YK, reason="Testing with real Yubikey.")
def test_check_no_key_inserted_for_targets_should_raise_error(
    taf_happy_path, targets_yk
):
    targets_yk.insert()
    targets_yk.remove()
    with pytest.raises(taf.exceptions.YubikeyError):
        taf_happy_path.is_valid_metadata_yubikey("targets")


def test_check_targets_key_id_for_targets_should_return_true(
    taf_happy_path, targets_yk
):
    targets_yk.insert()
    assert taf_happy_path.is_valid_metadata_yubikey("targets", targets_yk.tuf_key)


def test_check_root_key_id_for_targets_should_return_false(taf_happy_path, root1_yk):
    root1_yk.insert()
    assert not taf_happy_path.is_valid_metadata_yubikey("targets", root1_yk.tuf_key)


def test_update_snapshot_valid_key(taf_happy_path, snapshot_key):
    start_date = datetime.datetime.now()
    interval = 1
    expected_expiration_date = to_tuf_datetime_format(start_date, interval)
    targets_metadata_path = Path(taf_happy_path.metadata_path) / "targets.json"
    old_targets_metadata = targets_metadata_path.read_bytes()
    taf_happy_path.update_snapshot_keystores(
        [snapshot_key], start_date=start_date, interval=interval
    )
    new_snapshot_metadata = str(Path(taf_happy_path.metadata_path) / "snapshot.json")
    signable = securesystemslib.util.load_json_file(new_snapshot_metadata)
    tuf.formats.SIGNABLE_SCHEMA.check_match(signable)
    actual_expiration_date = signable["signed"]["expires"]

    # Targets data should remain the same
    assert old_targets_metadata == targets_metadata_path.read_bytes()
    assert actual_expiration_date == expected_expiration_date


def test_update_snapshot_wrong_key(taf_happy_path, timestamp_key):
    with pytest.raises(taf.exceptions.InvalidKeyError):
        taf_happy_path.update_snapshot_keystores([timestamp_key])


def test_update_timestamp_valid_key(taf_happy_path, timestamp_key):
    start_date = datetime.datetime.now()
    interval = 1
    expected_expiration_date = to_tuf_datetime_format(start_date, interval)
    targets_metadata_path = Path(taf_happy_path.metadata_path) / "targets.json"
    snapshot_metadata_path = Path(taf_happy_path.metadata_path) / "snapshot.json"
    old_targets_metadata = targets_metadata_path.read_bytes()
    old_snapshot_metadata = snapshot_metadata_path.read_bytes()
    taf_happy_path.update_timestamp_keystores(
        [timestamp_key], start_date=start_date, interval=interval
    )
    new_timestamp_metadata = str(Path(taf_happy_path.metadata_path) / "timestamp.json")
    signable = securesystemslib.util.load_json_file(new_timestamp_metadata)
    tuf.formats.SIGNABLE_SCHEMA.check_match(signable)
    actual_expiration_date = signable["signed"]["expires"]

    assert actual_expiration_date == expected_expiration_date
    # check if targets and snapshot remained the same
    assert old_targets_metadata == targets_metadata_path.read_bytes()
    assert old_snapshot_metadata == snapshot_metadata_path.read_bytes()


def test_update_timestamp_wrong_key(taf_happy_path, snapshot_key):
    with pytest.raises(taf.exceptions.InvalidKeyError):
        taf_happy_path.update_timestamp_keystores([snapshot_key])


def test_update_targets_from_keystore_valid_key(taf_happy_path, targets_key):
    start_date = datetime.datetime.now()
    interval = 1
    expected_expiration_date = to_tuf_datetime_format(start_date, interval)

    taf_happy_path.update_targets_keystores(
        [targets_key], start_date=start_date, interval=interval
    )
    new_targets_data = str(Path(taf_happy_path.metadata_path) / "targets.json")
    signable = securesystemslib.util.load_json_file(new_targets_data)
    tuf.formats.SIGNABLE_SCHEMA.check_match(signable)
    actual_expiration_date = signable["signed"]["expires"]

    assert actual_expiration_date == expected_expiration_date


def test_update_targets_from_keystore_wrong_key(taf_happy_path, snapshot_key):
    with pytest.raises(taf.exceptions.InvalidKeyError):
        taf_happy_path.update_targets_keystores([snapshot_key])


def test_update_targets_valid_key_valid_pin(taf_happy_path, targets_yk):
    if targets_yk.scheme != DEFAULT_RSA_SIGNATURE_SCHEME:
        pytest.skip()
    targets_path = Path(taf_happy_path.targets_path)
    repositories_json_path = targets_path / "repositories.json"

    branch_id = "14e81cd1-0050-43aa-9e2c-e34fffa6f517"
    target_commit_sha = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    repositories_json_old = repositories_json_path.read_text()

    targets_data = {
        "branch": {"target": branch_id},
        "dummy/target_dummy_repo": {"target": {"commit": target_commit_sha}},
        "capstone": {},
    }
    yk.add_key_pin(targets_yk.serial, "123456")
    targets_yk.insert()
    public_key = targets_yk.tuf_key
    taf_happy_path.update_targets_yubikeys(
        [public_key], targets_data, datetime.datetime.now()
    )

    assert (targets_path / "branch").read_text() == branch_id
    assert target_commit_sha in (targets_path / "dummy/target_dummy_repo").read_text()
    assert (targets_path / "capstone").is_file()
    assert repositories_json_old == repositories_json_path.read_text()


@pytest.mark.skipif(TEST_WITH_REAL_YK, reason="Testing with real Yubikey.")
def test_update_targets_wrong_key(taf_happy_path, root1_yk):
    with pytest.raises(taf.exceptions.InvalidKeyError):
        root1_yk.insert()
        yk.add_key_pin(root1_yk.serial, "123456")
        taf_happy_path.update_targets_yubikeys([root1_yk.tuf_key])
