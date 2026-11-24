#!/usr/bin/env bash
# Série de recursos do app no robô: uma linha a cada 30 s.
#
#   tools/medir_recursos.sh http://<robô>:8042 90 > recursos.log    # 45 minutos
#
# Cada linha traz o tempo de pé do app, descritores abertos, sockets, threads,
# a fase da voz, quantos trechos o detector de fala já abriu e o piso de ruído.
# É o que mostra vazamento: descritor, socket ou thread que só sobe com o robô
# em repouso é defeito (foi assim que o 1.2.1 morreu, com SIGTRAP, sem FD).
set -uo pipefail
ROBO="${1:-http://reachy-mini.local:8042}"
VOLTAS="${2:-90}"
for _ in $(seq 1 "$VOLTAS"); do
    curl -s -m 6 "$ROBO/api/robot/status" | python3 -I -c '
import json, sys, time
try:
    d = json.load(sys.stdin)
    r, v = d["resources"], d["voice"]
    m = v.get("detector") or {}
    print(time.strftime("%H:%M:%S"), "uptime", int(d["uptime_s"]), "fd", r["fd_count"],
          "sock", r["sockets"], "threads", r["threads"], "fase", v.get("phase"),
          "trechos", m.get("segments"), "piso", m.get("floor"),
          "faltam", d.get("missing"), flush=True)
except Exception as e:
    print(time.strftime("%H:%M:%S"), "SEM RESPOSTA", type(e).__name__, flush=True)
'
    sleep 30
done
