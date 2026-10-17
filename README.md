---
title: Azure Foundry Global Standard Resiliency Lab
description: KR, UK, US, CA 및 유럽 확장을 위한 Python 기반 동일 모델과 혼합 모델 장애 전환 가이드
ms.date: 2026-10-16
---

## 목적

PTU 없이 `GlobalStandard` PayGo를 사용하는 글로벌 Agent의 장애 대응을 검증합니다.
동일 모델·버전의 리전 전환과, 명시적으로 승인한 타 모델 전환을 분리합니다.
KR, UK, US, CA 사용자와 향후 유럽 진출을 고려합니다.

> [!IMPORTANT]
> 이 프로젝트는 reference implementation입니다. 로컬 테스트는 실제 OpenAI Python SDK와
> 모의 HTTP/SSE를 사용합니다. 실제 Azure 장애, 처리량, 글로벌 사용자 TTFT를 검증한 결과가 아닙니다.
> 유료 배포나 실호출은 자동 실행하지 않습니다.

## 읽는 순서

1. [2026년 리서치와 Top 3](docs/research.md)
2. [동일 모델·혼합 모델 및 다중 구독 아키텍처](docs/architecture.md)
3. [글로벌 사용자 TTFT 설계와 측정](docs/latency.md)
4. [Azure 구성·권한·운영 절차](docs/operations.md)
5. [테스트 범위와 재현 절차](docs/testing.md)
6. [고객 공유용 요약](docs/customer-summary.md)

![현재 단일 구독 글로벌 아키텍처](docs/diagrams/global-single-subscription.svg)

[다중 구독 SVG](docs/diagrams/global-multi-subscription.svg)는 향후 확장 참고안입니다.
현재 구현·로컬 테스트의 기본값은 단일 구독이며, 추가 구독 작업은 보류합니다.

## 지원 모델

조사일의 공식 Global Standard 지역 가용성 표 기준입니다.
모델 ID와 Azure deployment name은 서로 다릅니다. 호출에는 설정의 `deployment`를 사용합니다.

| 모델 ID        | 고정 버전   |
|----------------|-------------|
| gpt-6.1-sol    | 2026-09-29  |
| gpt-6-luna     | 2026-09-22  |
| gpt-5.6-terra  | 2026-07-09  |
| gpt-5.6-luna   | 2026-07-09  |

지원 표에 있어도 고객 구독의 quota, capacity, 모델 접근 승인까지 보장되지는 않습니다.
[공식 지역 가용성](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability#global-standard)을 배포 전에 다시 확인하세요.

## 빠른 실행

Python 3.11 이상과 [uv](https://docs.astral.sh/uv/)를 사용합니다. 다음은 Windows PowerShell 명령입니다.

```powershell
uv sync
uv run python -m foundry_resiliency simulate --count 3 --output artifacts\simulation.json
uv run python -m foundry_resiliency simulate --stream --count 3 --output artifacts\streaming.json
uv run python -m pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

시뮬레이션은 네 모델 × 동일/혼합 두 모드 × 8개 장애 조건을 각각 검증합니다.
`passed`는 장애 조건별 기대 동작을 만족했다는 의미입니다.
전체 용량 풀 장애에서는 성공 응답이 아니라 제한된 시간 내 명시적인 실패가 정답입니다.

## 예제 설정

[단일 구독 동일 모델 설정](configs/same-model.example.json)과
[혼합 모델 설정](configs/mixed-model.example.json)은 4개 리전과 4개 모델을 포함합니다.
모든 endpoint와 ARM resource ID는 교체해야 하는 예시입니다.
혼합 예제의 fallback 순서는 기능 시연용이며 고객 업무 품질 승인을 의미하지 않습니다.

```powershell
uv run python -m foundry_resiliency validate-config --config configs\same-model.example.json
uv run python -m foundry_resiliency example --mode mixed `
  --output configs\single-subscription.local.json
```

타 모델 전환에는 설정 allowlist와 요청의 `allow_model_fallback=True`가 모두 필요합니다.
CLI에서는 `--allow-model-fallback`으로 요청 단위 승인을 표현합니다.

## 반드시 알아둘 한계

* Global Standard의 리소스 리전은 실제 추론 수행 리전의 보장이 아닙니다.
* 서로 다른 리소스·리전·구독이 독립된 글로벌 용량 풀이라는 보장은 없습니다.
* 2026년 신규 quota 체계에 편입된 모델은 같은 구독의 모든 리전에서
  동일 모델·버전의 Global Standard quota를 공유합니다. Foundry Quota의 `Scope`를 확인하세요.
* 일부 기존 모델은 여전히 subscription/region/model/deployment-type 범위입니다.
  리전 수를 TPM 배수로 계산하지 마세요.
* 다중 구독은 별도 승인 quota 활용에 도움이 되지만 shared-pool throttling 해결을 보장하지 않습니다.
  문서의 usage-tier latency 기준은 tenant 전체의 모델 사용량을 합산합니다.
* 라우터의 headroom과 circuit state는 프로세스 내부 상태입니다.
  운영 다중 replica에는 공유 admission budget과 지역별 ingress 제한을 별도 구현해야 합니다.
* 텍스트 Responses API만 구현합니다. Tools, 이미지, 오디오, 서버측 conversation,
  Agent thread 이전, exactly-once 실행은 구현하지 않습니다.
* 첫 텍스트 delta 이후에는 stream을 다른 모델·리전으로 자동 재생하지 않습니다.
* 토큰 사전 추정은 admission 보조 수단이며 Azure의 rate-limit 계산을 정확히 복제하지 않습니다.

## Azure 실환경 실행

유료 리소스 생성은 [Bicep 템플릿](infra/main.bicep)을 검토한 후 별도로 수행합니다.
기존 배포가 있으면 [운영 가이드](docs/operations.md)의 권한 및 네트워크 점검을 먼저 진행합니다.

```powershell
uv run python -m foundry_resiliency preflight --config configs\single-subscription.local.json
uv run python -m foundry_resiliency smoke --config configs\single-subscription.local.json `
  --model gpt-6.1-sol --market KR --stream --count 5 --confirm-paid `
  --output artifacts\live-kr-sol.json
```

`smoke`는 ARM SKU·모델·버전·리전·endpoint 일치 확인 후 소량의 실제 요청을 보냅니다.
이는 글로벌 부하 테스트가 아닙니다. 실제 TTFT는 각 국가의 probe에서 반복 측정해야 합니다.

## 저장소 범위

코드는 [Python 패키지](src/foundry_resiliency/), 검증은 [tests](tests/),
설정은 [configs](configs/), 배포 템플릿은 [infra](infra/)에 있습니다.
외부 참고 repo의 소스를 복제하지 않고 패턴을 참고해 작성했습니다.
별도 GitHub repository 생성·게시나 기존 상위 저장소의 커밋은 자동 수행하지 않습니다.
