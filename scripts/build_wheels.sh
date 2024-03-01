#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

set -euo pipefail

if [ ! -d wheels ]; then
    mkdir wheels
fi
rm -f wheels/*

for dir in ../openjd-adaptor-runtime-for-python ../deadline-cloud ../deadline-cloud-for-cinema-4d; do
    echo "Building $dir..."    
    cd $dir
    hatch build
    mv dist/*.whl ../deadline-cloud-for-cinema-4d/wheels/
done
