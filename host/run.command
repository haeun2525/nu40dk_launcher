#!/bin/zsh
# 더블클릭으로 런처 실행. 터미널 창이 뜨고 거기서 버튼 입력을 기다린다.
cd "$(dirname "$0")"
exec /usr/bin/python3 launcher.py
