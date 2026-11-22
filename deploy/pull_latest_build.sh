#!/bin/bash
# Food Cat: take the latest successful build from S3 and install it on this server.
#
# GitHub Actions sends this file to the EC2 server through AWS Systems Manager (Run Command),
# so it runs on the server as root. Before sending it, the pipeline fills in the two settings below.
# The upload job writes builds/latest.txt only after the zip itself is safely in S3, so the pointer
# always names the newest build that passed the tests and the security scans.
set -euo pipefail

export AWS_DEFAULT_REGION=''
BUCKET=''

if [ -z "$BUCKET" ] || [ -z "$AWS_DEFAULT_REGION" ]; then
  echo "The bucket or the region was not filled in by the pipeline."
  exit 1
fi

KEY=$(aws s3 cp "s3://$BUCKET/builds/latest.txt" - | tr -d '[:space:]')
case "$KEY" in
  builds/build-v*.zip) ;;
  *) echo "builds/latest.txt does not name a build zip: '$KEY'"; exit 1 ;;
esac
NAME=$(basename "$KEY" .zip)
case "$NAME" in
  *[!A-Za-z0-9.-]*) echo "Unexpected build name: '$NAME'"; exit 1 ;;
esac

WORK=$(mktemp -d /var/tmp/foodcat-deploy.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

echo "Latest build in S3: s3://$BUCKET/$KEY"
aws s3 cp "s3://$BUCKET/$KEY" "$WORK/build.zip" --only-show-errors
python3 -m zipfile -e "$WORK/build.zip" "$WORK/$NAME"
bash "$WORK/$NAME/deploy/install_build.sh" "$WORK/$NAME"
