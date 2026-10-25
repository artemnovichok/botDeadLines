#!/bin/zsh
# Двойной щелчок в Finder открывает Terminal и запускает Docker-установку на этом Mac.
set -eu
umask 077
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd -- "${0:A:h}"

finish() {
    local result=$?
    trap - EXIT
    if (( result != 0 )); then
        print ""
        print "Запуск не завершён. Причина указана выше."
    fi
    if [[ -t 0 ]]; then
        print ""
        read -r "reply?Нажмите Enter, чтобы закончить. " || true
    fi
    exit "$result"
}
trap finish EXIT

print "Deadline Assistant — запуск через Docker на этом Mac"
print ""
if ! command -v docker >/dev/null 2>&1; then
    print "Не найден Docker. Установите и откройте Docker Desktop."
    exit 3
fi
if [[ -x .venv/bin/python ]]; then
    .venv/bin/python scripts/macos_docker.py start
elif command -v python3 >/dev/null 2>&1; then
    python3 scripts/macos_docker.py start
else
    print "Нужен Python 3.9 или новее для файла запуска (обычно он уже есть в macOS)."
    exit 3
fi
