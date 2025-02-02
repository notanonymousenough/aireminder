#!/bin/bash

set -e

VM_USER=$1
VM_HOST=$2

# Запускаем создание релиза
./release.sh

# Получаем последний созданный тег
LATEST_TAG=$(git describe --tags --abbrev=0)

# Деплоим последний тег
./deploy.sh $LATEST_TAG $VM_USER $VM_HOST