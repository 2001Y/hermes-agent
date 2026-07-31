# Hermes self-improvement audit — 2026-08-01

測定時点: `2026-08-01T04:18:45+09:00`

## 確認した事実

- Gateway: PID `2858`, LaunchAgent実行環境は `.../hermes-agent/venv`。
- macOS `maxfiles`: soft `256`, hard `unlimited`。
- GatewayのFDはread-only観測で `133`個、最大番号`218`。内訳は `PIPE 47`, `KQUEUE 15`, `unix 32` など。現時点で`EMFILE`は再現していない。
- `tools/process_registry.py`の終了済みPopen stdoutがFinished TTL中も保持され、長寿命GatewayのPIPE FDを蓄積し得る。reader finallyでstdoutをcloseする修正を実装した。
- `state.db`は約`10.35 GiB`。`dbstat`の概算は `messages_fts_trigram 5.17 GiB`, `messages 2.67 GiB`, `messages_fts 2.26 GiB`, `sessions 352 MiB`。
- `sessions prune --older-than 90d --dry-run`: 対象なし。
- `source=cron --older-than 14d --dry-run`: `37`件。うち四半期監査・claim jobを含むため、保持期間を決めず削除していない。
- `VACUUM`はDB再書込みに約`20.74 GiB`を必要とし、空き`6.23 GiB`のためpreflightで拒否される。
- `hermes doctor`は大DBで全量FTS/write probeを実行せず、bounded read probeへ切り替えた。実行時に`state.db full FTS probe skipped for bounded doctor`を表示し、約10秒で終了した。
- 既存圧縮ログ46件: median `39.788s`, p90 `251.401s`, 入力token median `233,818`、rough output token median `123,251`。全件`awaiting_real_usage=true`で、実使用量の確定値は未記録。

## 実装した変更

- 大DB向け`hermes doctor` bounded state DB probe。
- `SessionDB.vacuum_preflight()`と、空き容量不足時のfail-closedな`VACUUM`拒否。
- `hermes sessions optimize --dry-run`。
- `run_tests.sh`がpytestのないvenvを選ばないよう修正。
- process-registry reader終了時のPopen stdout close。
- 共通スコアを作らない、FD/DB/command/log adapter方式の`hermes_cli/perf_probe.py`とCLI wrapper。
- `agent.max_turns=9999`→`90`、`delegation.max_iterations=9999`→`50`、`delegation.max_concurrent_children=99`→`3`。
- memory/skill write approvalを有効化。

## 破壊的操作・未反映

- session削除、VACUUM、Gateway再起動は実施していない。
- 設定・コード変更はファイルへ書き込んだが、稼働中Gateway PID `2858`へはまだ反映していない。
- 圧縮modelは`gpt-5.4-mini`へ戻した。現在の`openai-codex` routeはusage-pricing上`subscription_included`であり、Lunaの方が安いとは判定できない。Lunaを使うA/Bは、実使用量・品質を同じworkloadで取得してから行う。

## 検証

- doctor focused tests: pass
- process registry + perf probe: `116 passed`
- state vacuum/FTS focused tests: `8 passed`
- checkpoint manager: `77 passed`
- credential-related selected tests: `123 passed`
- write approval: `34 passed`
- usage pricing: `31 passed`
- changed files: `ruff check` pass
- 全体test suite: `29`件の失敗、2ファイル未実行。失敗はmacOS上にない`systemctl`、`/tmp`→`/private/tmp`正規化、実バイナリの挙動、live-system guard、provider/環境依存など今回変更対象外を含む。今回変更対象のfocused testsはすべてpass。
- `hermes gateway restart`は、実行元がGateway子プロセスのため安全ガードにより拒否された。設定・コード変更をlive PIDへ反映するには、Gateway外の別シェルで実行する必要がある。

## 実行しなかった破壊的操作

- 37件のcron session削除: 保持期間が未指定で、四半期監査・claim jobを含むため未実施。
- `VACUUM`: preflightで空き容量不足（必要約`20.74 GiB`、空き約`6.23 GiB`）のため未実施。
- state.dbの10GiBバックアップ: 現在の空き容量では安全なコピー余地がないため未実施。
