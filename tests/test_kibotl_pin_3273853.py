# SPDX-License-Identifier: GPL-3.0-or-later
"""The built-in KiboTL pin is the 2026-11-27 16:13 UTC re-upload of en.zip, for game build 3273853.

The 11-27 resource update renamed three containers (the main string table, the data table and
the static text), so their destinations and before pins moved. Only those three members are
pinned: the upload's startup.r and onest-font.r do not fit the 11-24 client's StreamingAssets
(tested on a real install: images and text land in the wrong places), so they stay out until
KiboTL ships ones that do.
"""

from nadare.core import patchspecs

RES = "AzurPromiliaJP_Data/.res/"
PINS = {
    "vstring.bytes": (
        RES + "raw_base_package/BundleFiles/heqmts9lcjdvbupu0x2wpg.r",
        "1dab6f55d9f8c339cad95845390354f1504d51f033bd523bffa02dbcba4813ea",
        140506137,
        "60e09fa31ac7e43e2a74cb7e722cf19b3331a188817125a086a57a17a34987ee",
        134055486,
    ),
    "table_data.bytes": (
        RES + "raw_streaming_package/BundleFiles/5g1m5b4sbqlfalcxoaquw.r",
        "b6255ead4d4e2b8ea9cd9442e78c8b619433747edef08b42ed471081f9fb500f",
        28840130,
        "4369839fd46cd2c821f33dc2b92c46cc87f4413a215be0abe17461ec743896c2",
        28840130,
    ),
    "static-text.r": (
        RES + "default_package/BundleFiles/h3y7ujukwn9qbhoqhzkkg.r",
        "02aaed5b1a61370e356aced8b52b87d8872bf491204cae2ce5b35f07cb69afc0",
        4991773,
        "9771462bacb96c7ee544fe53d0d2a4e56e3576b1336d2a2ec6653bfef1a6a138",
        4993822,
    ),
}


def test_the_builtin_pin_is_the_1127_1613_upload_for_build_3273853():
    spec = patchspecs.KIBOTL_EN
    assert spec.game_build == "3273853"
    assert spec.archive_sha256 == "bb145e147e59ade1a327ee4b620d1412b17042b329febda456f50f901a46b043"
    assert spec.archive_size == 209693417


def test_only_the_three_text_containers_are_pinned():
    got = {
        f.member: (f.dest, f.sha256, f.size, f.before_sha256, f.before_size)
        for f in patchspecs.KIBOTL_EN.files
    }
    assert got == PINS


def test_the_version_names_the_build_it_fits():
    # The upstream tag still says 3219598; the version is what Nadare shows and compares.
    assert patchspecs.KIBOTL_EN.version.endswith("3273853")
