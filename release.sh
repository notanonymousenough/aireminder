#!/bin/bash

if git diff --quiet && [[ $(git branch --show-current) == "master" ]]; then
    echo "No uncommitted changes and on master branch."
    exit 0
else
    echo "Error: Either there are uncommitted changes or not on master branch." >&2
    exit 1
fi

# Генерируем уникальный тег (дата + короткий хеш коммита)
RELEASE_TAG="release-$(date +%Y%m%d%H%M%S)-$(git rev-parse --short HEAD)"

# Создаем и пушим тег
git tag -a $RELEASE_TAG -m "Release $RELEASE_TAG"
git push origin $RELEASE_TAG

echo "Created and pushed release tag: $RELEASE_TAG"