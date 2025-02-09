#!/bin/bash

set -e

# Deploy Tag
TARGET_TAG=$1
# SSH options
VM_USER=$2
VM_HOST=$3
PROJECT_PATH="/opt/aireminder"

# Выполняем команды на VM через SSH
ssh $VM_USER@$VM_HOST << EOF
  cd $PROJECT_PATH
  git fetch --all --tags
  git checkout tags/$TARGET_TAG
  .venv/bin/pip install -r requirements.txt
  pkill -f "python main.py"
  export TZ="Europe/Moscow" && nohup .venv/bin/python main.py > main.log 2>&1 &
  echo "Successfully deployed tag $TARGET_TAG"
EOF