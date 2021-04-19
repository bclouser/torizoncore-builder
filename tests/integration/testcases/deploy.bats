load 'bats/bats-support/load.bash'
load 'bats/bats-assert/load.bash'
load 'bats/bats-file/load.bash'

@test "deploy: run without parameters" {
    run torizoncore-builder deploy
    assert_failure 255
    assert_output --partial "One of the following arguments is required: --output-directory, --remote-host"
}

@test "deploy: check help output" {
    run torizoncore-builder deploy --help
    assert_success
    assert_output --partial "usage: torizoncore-builder deploy"
}

@test "deploy: deploy changes to Tezi image" {
    local ROOTFS=temp_rootfs
    torizoncore-builder-shell "rm -Rf /workdir/my_new_image"
    rm -rf $ROOTFS

    torizoncore-builder-clean-storage
    torizoncore-builder images --remove-storage unpack $DEFAULT_TEZI_IMAGE
    torizoncore-builder union --changes-directory $SAMPLES_DIR/changes --union-branch branch1

    run torizoncore-builder deploy --output-directory my_new_image branch1
    assert_success
    assert_output --partial "Packing rootfs done."

    mkdir $ROOTFS && cd $ROOTFS
    tar -I zstd -xvf ../my_new_image/*.ota.tar.zst
    run cat ostree/deploy/torizon/deploy/*/etc/myconfig.txt
    assert_success
    assert_output --partial "enabled=1"
    cd .. && rm -rf $ROOTFS
}

@test "deploy: deploy changes to device" {
    requires-device

    torizoncore-builder-clean-storage
    torizoncore-builder images --remove-storage unpack $DEFAULT_TEZI_IMAGE
    torizoncore-builder union --changes-directory $SAMPLES_DIR/changes2 --union-branch branch1

    run torizoncore-builder deploy --remote-host $DEVICE_ADDR --remote-username $DEVICE_USER --remote-password $DEVICE_PASS --reboot branch1
    assert_success
    assert_output --partial "Deploying successfully finished"

    run device-wait 10
    assert_success

    run device-shell /usr/sbin/secret_of_life
    assert_failure 42
}