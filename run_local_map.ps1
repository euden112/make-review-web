<#
로컬 Map → 클라우드 Reduce 일괄 실행 래퍼 (모드 B)

.env에서 CLOUD_URL / API_SECRET_KEY를 자동 로드한 뒤, 지정한 게임을 순차 처리한다.
run_map_pipeline.py 자체는 .env를 읽지 않으므로(셸 환경변수만 사용) 이 래퍼가 대신 로드한다.

사용 예:
  .\run_local_map.ps1 -Games "1-50"            # 1~50번 게임, 첫 요약(force) — map+reduce
  .\run_local_map.ps1 -Games "1,2,5,8"         # 특정 게임만
  .\run_local_map.ps1 -Games all               # 클라우드의 전체 게임
  .\run_local_map.ps1 -Games "1-50" -Model qwen2.5:3b   # 더 빠른 모델
  .\run_local_map.ps1 -Games "10-20" -Force:$false      # 증분(커서 이후만)
  .\run_local_map.ps1 -Games "1-50" -Replay    # 저장된 payload로 map 스킵, reduce만 재전송
  .\run_local_map.ps1 -Games "1-10" -Replay -ReplayDelaySeconds 120  # RPM/TPM 보호용 간격

-Replay: 이미 저장된 reduce payload(ai-pipeline/artifacts/reduce_payloads/keep)를 게임별
최신본으로 골라 /reduce에만 재전송한다. map(로컬 GPU) 단계를 건너뛰므로 reduce-side
변경(vague 필터, aspect baseline, 점수 공식 등)을 빠르게 일괄 재적용할 때 쓴다.
주의: map-side 변경(polarity, 청크, 버킷 임계)은 payload에 동결돼 있어 반영 안 됨 → 그땐 full 실행.
#>
param(
  [string]$Games = "1-50",        # "1-50" | "1,2,5" | "all"
  [string]$Model = "gemma4:e4b",  # 로컬 Ollama 모델
  [bool]$Force = $true,           # 첫 요약은 $true(전체 재처리), 증분은 $false
  [switch]$Replay,                # 저장된 payload로 reduce만 재전송(map 스킵)
  [int]$ReplayDelaySeconds = 0,    # Replay 게임별 reduce 등록 후 대기(429/RPM/TPM 완화)
  [string]$PayloadDir = (Join-Path $PSScriptRoot "ai-pipeline\artifacts\reduce_payloads\keep")
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# 1) .env → 세션 환경변수 로드
$envPath = Join-Path $root ".env"
if (-not (Test-Path $envPath)) { Write-Error ".env 없음: $envPath"; exit 1 }
Get-Content $envPath | ForEach-Object {
  if ($_ -match '^\s*([^#=]+)=(.*)$') { Set-Item "env:$($matches[1].Trim())" $matches[2].Trim() }
}
if (-not $env:CLOUD_URL)      { Write-Error "CLOUD_URL 미설정 (.env 확인)"; exit 1 }
if (-not $env:API_SECRET_KEY) { Write-Error "API_SECRET_KEY 미설정 (.env 확인)"; exit 1 }
Write-Host "CLOUD_URL = $($env:CLOUD_URL)" -ForegroundColor DarkGray

# 2) 게임 id 목록 산출
function Resolve-Ids([string]$spec) {
  if ($spec -eq "all") {
    return (Invoke-RestMethod "$($env:CLOUD_URL)/api/v1/games/").id
  }
  $ids = @()
  foreach ($part in ($spec -split ',')) {
    $p = $part.Trim()
    if ($p -match '^(\d+)-(\d+)$')  { $ids += [int]$matches[1]..[int]$matches[2] }
    elseif ($p -match '^\d+$')      { $ids += [int]$p }
  }
  return $ids
}
$ids = Resolve-Ids $Games
if (-not $ids) { Write-Error "처리할 게임 id 없음: '$Games'"; exit 1 }
Write-Host ("처리 대상 {0}개: {1}" -f $ids.Count, ($ids -join ',')) -ForegroundColor Cyan

# 게임별 최신 payload 파일 경로를 돌려준다(없으면 $null).
function Get-LatestPayload([int]$gameId) {
  $cands = Get-ChildItem (Join-Path $PayloadDir "game_${gameId}_*.json") -ErrorAction SilentlyContinue
  if (-not $cands) { return $null }
  return ($cands | Sort-Object LastWriteTime | Select-Object -Last 1).FullName
}

$ok = 0; $fail = @(); $skip = @()
if ($Replay) {
  # 3a) Replay: map 스킵, 저장된 payload로 reduce만 재전송
  if (-not (Test-Path $PayloadDir)) { Write-Error "payload 디렉터리 없음: $PayloadDir"; exit 1 }
  Write-Host "REPLAY 모드 — payload 디렉터리: $PayloadDir" -ForegroundColor Magenta
  for ($i = 0; $i -lt $ids.Count; $i++) {
    $id = $ids[$i]
    $pl = Get-LatestPayload $id
    if (-not $pl) { $skip += $id; Write-Host "  SKIP game $id (payload 없음)" -ForegroundColor DarkYellow; continue }
    Write-Host "=== replay game $id ===" -ForegroundColor Cyan
    python run_map_pipeline.py --from-payload $pl
    if ($LASTEXITCODE -eq 0) { $ok++ }
    else { $fail += $id; Write-Host "  FAIL game $id (exit $LASTEXITCODE)" -ForegroundColor Red }
    if ($ReplayDelaySeconds -gt 0 -and $i -lt ($ids.Count - 1)) {
      Write-Host ("  wait {0}s before next replay" -f $ReplayDelaySeconds) -ForegroundColor DarkGray
      Start-Sleep -Seconds $ReplayDelaySeconds
    }
  }
} else {
  # 3b) Full: 게임 순차 map+reduce (1개씩 → 로컬 GPU 직렬, 동시성/터널 타임아웃 없음)
  foreach ($id in $ids) {
    Write-Host "=== game $id ===" -ForegroundColor Cyan
    $a = @("run_map_pipeline.py", "--game-id", $id, "--map-route", "local", "--model", $Model)
    if ($Force) { $a += "--force" }
    python @a
    if ($LASTEXITCODE -eq 0) { $ok++ }
    else { $fail += $id; Write-Host "  FAIL game $id (exit $LASTEXITCODE)" -ForegroundColor Red }
  }
}

Write-Host ("완료: {0} ok / {1} fail / {2} skip" -f $ok, $fail.Count, $skip.Count) -ForegroundColor Green
if ($fail) { Write-Host ("실패 게임: {0}" -f ($fail -join ',')) -ForegroundColor Yellow }
if ($skip) { Write-Host ("스킵 게임(payload 없음): {0}" -f ($skip -join ',')) -ForegroundColor Yellow }
