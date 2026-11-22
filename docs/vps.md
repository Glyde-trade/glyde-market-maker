# Run on a VPS

Choose **one** recipe: Docker Compose or a Linux systemd service. Both examples start
in paper mode, persist `.glyde-mm`, bound console logs, and disable automatic restarts.
They require v0.3.0 or current source, including `stop`; the v0.2.0 wheel predates that command.
Review and pin the source commit you deploy. These examples do not provision an account,
grant credentials or qualify a host for live trading.

## Safety checklist

- Use a dedicated account, an authorized trading API key and the separate read credential.
  Never use a main-wallet seed phrase. Keep key **values only in the private env file**,
  mode `0600`; YAML contains public settings and environment-variable names only.
- Set exact owner/vault addresses from account discovery. Review order size, position,
  notional and daily-loss limits against the current market. Loss thresholds are not guarantees.
- Run doctor and paper first. Ordinary doctor does not send keys; explicit `--private`
  only checks reads and does not prove order authorization.
- Keep one runner per dedicated account. Use the same config, state directory and Compose
  project for start, stop and report. Other hosts/older bots cannot be stopped by a local lock.
- Preserve state and inspect the venue after any unconfirmed cancellation. Do not delete
  risk files, change volume names or restart to escape a risk/unknown-outcome stop.
- Protect host access, disk and backups. Root and Docker operators can read container
  environment variables; `0600` is not a defense against a compromised host administrator.

## Docker Compose

Install Docker Engine with Compose following the [Docker installation guide](https://docs.docker.com/engine/install/).
Clone the repository, review its revision, and run the following from the repository root:

```sh
mkdir -p "$HOME/.config/glyde-mm"
chmod 0700 "$HOME/.config/glyde-mm"
cp examples/paper.yaml "$HOME/.config/glyde-mm/bot.yaml"
chmod 0644 "$HOME/.config/glyde-mm/bot.yaml"
touch "$HOME/.config/glyde-mm/bot.env"
chmod 0600 "$HOME/.config/glyde-mm/bot.env"
export GLYDE_MM_CONFIG="$HOME/.config/glyde-mm/bot.yaml"
export GLYDE_MM_ENV_FILE="$HOME/.config/glyde-mm/bot.env"
docker compose build bot
docker compose run --rm bot doctor /work/bot.yaml
docker compose up -d bot
docker compose logs --tail=50 -f bot
```

Paper needs no credentials: leave the env file empty. For deliberate live use, edit it
in an editor, using the names referenced by your config, not `export` commands:

```text
GLYDE_API_PRIVATE_KEY=<trading API key>
```

The bot mints its own short-lived read credentials from the trading key; set
`GLYDE_READ_API_KEY` only to override that with a credential you minted elsewhere.

Keep that file outside the repository/build context. Never paste actual keys into YAML,
Compose arguments, screenshots or issues. Do not post `docker compose config` output:
it can expand env-file values. Docker mounts the public config read-only; the existing
image runs as UID `10001` and initializes the named state volume for that user.
Console logs rotate at three 10 MB files per container, using
[Compose logging options](https://docs.docker.com/reference/compose-file/services/#logging).
The application separately rotates each run's private diagnostics; monitor total disk
usage across runs and keep risk state when archiving older diagnostic directories.

Stop and inspect the report, using the **same exports and project directory**:

```sh
docker compose run --rm bot stop /work/bot.yaml
docker compose stop bot
docker compose run --rm bot report /work/.glyde-mm/logs
```

`stop` prints the path of its private cancellation report under `/work/.glyde-mm/stops`.
A failure is not proof that orders are gone. Check the venue before restarting.
Never use `docker compose down --volumes` or prune the state volume to clear a refusal.

### Deliberate live session

Stop paper first, review your config and explicitly change its mode to `live`; add the
registered owner/vault and small reviewed limits. With the private env file in place:

```sh
docker compose run --rm bot doctor /work/bot.yaml
docker compose run --rm bot doctor /work/bot.yaml --private
docker compose run --rm --name glyde-mm-live bot run /work/bot.yaml
```

This runs interactively: read the summary and type the exact symbol. From another
terminal, restore the same two path exports and use the `stop` command above. Do not
start the default paper service alongside live, pipe a confirmation into stdin or add
an automatic restart policy. Existing symbol exposure requires a separate deliberate
`--i-understand` decision; this recipe does not grant that override.

## systemd paper service

This example targets Linux with Python 3.11+, `python3-venv`, Git and systemd with
`LogNamespace` support (245+). It is intentionally **paper only**: live startup requires
interactive typed-symbol confirmation, which an unattended service cannot provide.
Do not replace confirmation with an `echo`/`yes` pipeline. Use the interactive recipe
above for live until you deliberately design a supervised interactive launch.

From the reviewed repository checkout, create the service user and install current source:

```sh
sudo useradd --system --home-dir /var/lib/glyde-mm --shell /usr/sbin/nologin glyde-mm
sudo python3 -m venv /opt/glyde-mm/venv
sudo /opt/glyde-mm/venv/bin/python -m pip install .
sudo install -d -m 0700 -o glyde-mm -g glyde-mm /var/lib/glyde-mm
sudo install -m 0644 examples/paper.yaml /var/lib/glyde-mm/bot.yaml
sudo install -d -m 0700 /etc/glyde-mm
sudo touch /etc/glyde-mm/bot.env
sudo chmod 0600 /etc/glyde-mm/bot.env
sudo install -m 0644 deploy/glyde-mm.service /etc/systemd/system/glyde-mm.service
sudo install -m 0644 deploy/journald@glyde-mm.conf /etc/systemd/journald@glyde-mm.conf
sudo systemctl daemon-reload
sudo systemctl start glyde-mm.service
sudo journalctl --namespace=glyde-mm -u glyde-mm.service --lines=50 --follow
```

Skip user creation if that dedicated user already exists. Leave the root-owned env
file empty for paper. systemd reads it before changing to the service user; do not
make the secrets file world-readable to solve a permission error. The paper YAML must
stay `mode: paper` so `ExecStop` addresses the same local session as `ExecStart`.

The [unit execution settings](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
isolate writable state in `/var/lib/glyde-mm`. The dedicated journal namespace limits
its own logs to 100 MB on disk, 20 MB in memory and seven days of retention. It does
not change another service's journal policy. Application diagnostic logs and risk state
remain under `/var/lib/glyde-mm/.glyde-mm`; monitor and back them up separately.

Stop and read the offline diagnostic report:

```sh
sudo systemctl stop glyde-mm.service
sudo -u glyde-mm /opt/glyde-mm/venv/bin/glyde-mm report /var/lib/glyde-mm/.glyde-mm/logs
```

Inspect `stops/` and the venue after an unconfirmed stop. No automatic restart or boot
enablement is configured; only enable boot startup after reviewing paper behavior and
the host's network/storage availability. Preserve config, env file and state during an
upgrade, stop first, then install the new reviewed revision and start explicitly.
