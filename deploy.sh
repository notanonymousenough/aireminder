#!/bin/bash

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
  pip install -r requirements.txt
  pkill -f "python3.12 main.py"
  nohup python3.12 main.py > main.log 2>&1 &
  echo "Successfully deployed tag $TARGET_TAG"
EOF