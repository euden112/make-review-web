<#
로컬 Map → 클라우드 Reduce 일괄 실행 래퍼 (모드 B)

.env에서 CLOUD_URL / API_SECRET_KEY를 자동 로드한 뒤, 지정한 게임을 순차 처리한다.
run_map_pipeline.py 자체는 .env를 읽지 않으므로(셸 환경변수만 사용) 이 래퍼가 대신 로드한다.

사용 예:
  .\run_local_map.ps1 -Games "1-50"            # 1~50번 게임, 첫 요약(force)
  .\run_local_map.ps1 -Games "1,2,5,8"         # 특정 게임만
  .\run_local_map.ps1 -Games all               # 클라우드의 전체 게임
  .\run_local_map.ps1 -Games "1-50" -Model qwen2.5:3b   # 더 빠른 모델
  .\run_local_map.ps1 -Games "10-20" -Force:$false      # 증분(커서 이후만)
#>
param(
  [string]$Games = "1-50",        # "1-50" | "1,2,5" | "all"
  [string]$Model = "gemma4:e4b",  # 로컬 Ollama 모델
  [bool]$Force = $true            # 첫 요약은 $true(전체 재처리), 증분은 $false
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

# 3) 게임 순차 실행 (1개씩 → 로컬 GPU 직렬, 동시성/터널 타임아웃 없음)
$ok = 0; $fail = @()
foreach ($id in $ids) {
  Write-Host "=== game $id ===" -ForegroundColor Cyan
  $a = @("run_map_pipeline.py", "--game-id", $id, "--map-route", "local", "--model", $Model)
  if ($Force) { $a += "--force" }
  python @a
  if ($LASTEXITCODE -eq 0) { $ok++ }
  else { $fail += $id; Write-Host "  FAIL game $id (exit $LASTEXITCODE)" -ForegroundColor Red }
}

Write-Host ("완료: {0} ok / {1} fail" -f $ok, $fail.Count) -ForegroundColor Green
if ($fail) { Write-Host ("실패 게임: {0}" -f ($fail -join ',')) -ForegroundColor Yellow }
