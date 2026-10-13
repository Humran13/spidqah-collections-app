# SPIDQAH Disaster Recovery Guide

This guide is written for someone who does not know PostgreSQL or
Docker. Follow the steps in order and copy-paste the commands exactly.

Two situations are covered:

- **Situation A** - your VPS is gone (destroyed, cancelled, unreachable)
  and you are starting over on a brand-new server.
- **Situation B** - your current SPIDQAH installation is broken, but
  the server itself is fine.

First, two words explained in plain language:

- **Live database** - the working copy of your data that PostgreSQL
  is actively using right now, stored in files under
  `/home/<your-site-user>/spidqah-data/postgres/`. You never touch
  these files directly.
- **Backup file** - a single file (`spidqah_full_<date>.tar.gz`) that
  contains a safe, frozen copy of your database, your uploaded files,
  and your app's settings, all bundled together. This is the file you
  download from Google Drive and the file you hand to the recovery
  command.

## Situation A: My VPS is completely destroyed

1. **Buy a new Ubuntu VPS** and set up CloudPanel on it, with the same
   domain (e.g. `col.therealworldboosts.com`) pointed at it in
   CloudPanel, exactly like before. Nothing SPIDQAH-specific here -
   this is the normal CloudPanel setup you've done before.

2. **Install SPIDQAH** with the same one-line command as always:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/Humran13/spidqah-collections-app/main/scripts/deploy-vps.sh | sudo bash
   ```
   It will ask for your domain - type the same domain as before (e.g.
   `col.therealworldboosts.com`). Let it finish completely. It will
   print a temporary Admin username/password - ignore those, you are
   about to bring back your real account.

3. **Download your latest backup from Google Drive.** Find the most
   recent `spidqah_full_<date>.tar.gz` file.

4. **Put it on the server**, in the backups folder the installer just
   created (replace `therealworldboosts-col` with your actual
   CloudPanel site username if different):
   ```bash
   # from your own computer, if the file is on your computer:
   scp spidqah_full_2026-10-01_020000.tar.gz root@YOUR_SERVER_IP:/home/therealworldboosts-col/spidqah-data/backups/
   ```
   (Or download it directly on the server with `rclone` / `wget` /
   whatever you normally use to pull from Google Drive - the important
   part is that the file ends up inside the `spidqah-data/backups/`
   folder.)

5. **Run the one recovery command** (as root, on the server):
   ```bash
   sudo bash /opt/spidqah-collections-app/scripts/recover.sh \
     --backup /home/therealworldboosts-col/spidqah-data/backups/spidqah_full_2026-10-01_020000.tar.gz
   ```
   This single command checks the backup is genuine and not corrupted,
   brings your database back exactly as it was, brings your uploaded
   files back, fixes any file-permission problems automatically, and
   restarts the website. It takes a few minutes. It prints a clear
   "SPIDQAH recovery successful" message with the counts of
   users/contributors/transactions it restored when it is done - if
   anything is wrong, it stops and tells you exactly what, instead of
   pretending everything is fine.

6. **Open your website** (`https://col.therealworldboosts.com`) and log
   in with your **original** username and password - the ones you were
   using before the disaster, not the temporary ones from step 2.
   Spot-check a contributor or transaction you remember to confirm your
   real financial records are back.

That's it. No manual SQL, no manual permission fixing, no guessing.

## Situation B: My current installation is broken, but the server is fine

Use this when the website is down, erroring, or behaving strangely,
but you haven't lost the server itself.

1. Make sure you have a recent backup file in
   `/home/<your-site-user>/spidqah-data/backups/` (run
   `scripts/backup-full.sh` yourself right now if you're not sure the
   last automatic one is recent enough - see "Making backups" below).

2. Run the exact same recovery command as in Situation A, step 5,
   pointing at whichever backup file you trust:
   ```bash
   sudo bash /opt/spidqah-collections-app/scripts/recover.sh --backup /home/<your-site-user>/spidqah-data/backups/spidqah_full_<date>.tar.gz
   ```

3. **Important**: if your current database already has real
   data in it (which it almost certainly does, since the server itself
   is fine), the command will refuse to run and tell you so - this is
   on purpose, to stop you from accidentally destroying today's data
   with an older backup. Only add `--force` if you are certain you want
   to throw away whatever is currently in the database and go back to
   the backup's version:
   ```bash
   sudo bash /opt/spidqah-collections-app/scripts/recover.sh --backup /home/<your-site-user>/spidqah-data/backups/spidqah_full_<date>.tar.gz --force
   ```

You do **not** need to reinstall Ubuntu, CloudPanel, or SPIDQAH itself
for this situation - the server, CloudPanel, and the installed app are
untouched; only the database and uploads are put back.

## Making backups

A full backup (everything needed for the recovery above) is made with:

```bash
sudo /opt/spidqah-collections-app/scripts/backup-full.sh
```

It writes a timestamped `spidqah_full_<date>.tar.gz` file into
`/home/<your-site-user>/spidqah-data/backups/`, keeps the 7 most recent
ones automatically (never deletes the only one left), and never
succeeds unless the archive it just wrote actually checks out.

**Run this daily**, shortly before whatever time your CloudPanel/rclone
backup job runs, so the backup file is sitting there ready to be
copied to Google Drive. Add it to root's crontab, e.g. for 2:00 AM if
your CloudPanel backup runs at 2:30 AM:

```
0 2 * * * /opt/spidqah-collections-app/scripts/backup-full.sh >> /var/log/spidqah-backup.log 2>&1
```

### Does CloudPanel actually back this up to Google Drive automatically?

**Not by itself - check this.** CloudPanel's own backup/rclone setup is
usually scoped to the website's `htdocs/` folder. The
`spidqah-data/` folder sits *next to* `htdocs/` under the same site
user's home directory, and is **not automatically included** unless
your backup configuration says so.

**Open your CloudPanel backup settings and confirm the backup path
covers this exact folder** (replace with your real site username):

```
/home/therealworldboosts-col/spidqah-data/
```

If your current backup job only lists `htdocs/`, add
`spidqah-data/` as an extra path. This is the one manual step that
makes everything else in this guide actually work when disaster
strikes - without it, your backups are being made on the server but
never leaving it. This project does not and will not modify your
CloudPanel or rclone configuration for you.

## The lightweight, database-only backup

`scripts/backup-db.sh` (older, still available) makes a quick
database-only dump, without uploads or a manifest. It's fine for extra
frequent snapshots between full backups, but **`recover.sh` needs a
full backup from `backup-full.sh`**, not a plain database-only one.

## What automatic recovery can and cannot guarantee

**It can:**
- Reject a backup file that is missing, empty, corrupted, truncated,
  or not actually a SPIDQAH backup, before touching anything.
- Detect if the backup was made on a different PostgreSQL major
  version than you're currently running, and refuse rather than risk a
  broken restore.
- Refuse to overwrite a database that already has real data in it,
  unless you explicitly pass `--force`.
- Restore the database as one all-or-nothing operation - if anything
  goes wrong partway through, PostgreSQL itself undoes the whole thing
  automatically, so you never end up with a half-restored database.
- Automatically fix file ownership/permissions for PostgreSQL and for
  uploads, detected from the actual running images (not a guess).
- Keep your old uploaded files (renamed aside, never deleted) if you
  restore over an installation that already had uploads.

**It cannot:**
- Recover data that was never in a backup file in the first place -
  it can only restore what you backed up, as recent as your last
  backup.
- Fix problems with CloudPanel, Nginx, SSL, DNS, or your domain - this
  tool only restores the SPIDQAH application's own data.
- Protect you if your only copy of a backup file is also lost (e.g. it
  was never actually copied to Google Drive) - that is why the
  CloudPanel backup-path check above matters.

## Troubleshooting

- **"No CloudPanel website found for domain..."** during install - you
  need to create the site in CloudPanel first, with that exact domain,
  before running the installer.
- **"this database already contains real data"** during recovery - see
  Situation B, step 3 above; this is the safety guard working as
  intended, not an error.
- **"PostgreSQL version mismatch"** - the backup was made on a
  different major PostgreSQL version than this server is running.
  Don't force past this; get the versions to match first.
- **Recovery says the database was restored but the website still
  won't load** - the database is safe either way; run
  `docker compose logs web` in `/opt/spidqah-collections-app` and get
  help with that specific error rather than re-running recovery again.
