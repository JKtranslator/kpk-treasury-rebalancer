# Opens the SSH tunnel that lets the page (local or https://jktranslator.github.io/kpk-treasury-rebalancer/)
# talk to the executor running on the OCI box. Keep this window open while you use Refresh / Execute.
#
#   powershell -ExecutionPolicy Bypass -File rebalancer\connect-oci.ps1
#
# The executor listens on 127.0.0.1:8743 on the box; this forwards your local 8743 to it. Nothing is
# exposed to the internet: the box accepts SSH from your IP only, and the executor binds to loopback.
param(
  [string]$KeyPath = "$HOME\.ssh\oracle_pmkt",
  [string]$Host_ = "ubuntu@82.70.94.93",
  [int]$Port = 8743
)
Write-Host "Tunnel 127.0.0.1:$Port -> OCI executor. Ctrl+C to close." -ForegroundColor Green
ssh -i $KeyPath -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -L "${Port}:127.0.0.1:${Port}" $Host_
