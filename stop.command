#!/bin/zsh
# Штатная остановка только Docker-контейнера этого проекта, без удаления данных.
set -eu
umask 077
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd -- "${0:A:h}"

finish() {
    local result=$?
    trap - EXIT
    if [[ -t 0 ]]; then
        print ""
        read -r "reply?Нажмите Enter, чтобы закончить. " || true
    fi
    exit "$result"
}
trap finish EXIT

print "Deadline Assistant — остановка Docker-контейнера"
print ""
if ! command -v docker >/dev/null 2>&1; then
    print "Docker не найден. Контейнер бота не запущен."
    exit 0
fi
if [[ -x .venv/bin/python ]]; then
    .venv/bin/python scripts/macos_docker.py stop
elif command -v python3 >/dev/null 2>&1; then
    python3 scripts/macos_docker.py stop
else
    print "Откройте Docker Desktop и остановите контейнер deadline-macos из этого проекта."
    exit 3
fi
