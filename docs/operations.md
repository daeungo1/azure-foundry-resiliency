---
title: 단일 구독 Azure 구성과 운영 절차
description: 모델 배포, 최소 권한, preflight, 소량 실호출과 장애 대응 runbook
ms.date: 2026-10-16
---

## 현재 실행 범위

현재는 단일 구독의 KR/UK/US/CA 리소스 구성을 대상으로 합니다.
다중 구독은 향후 확장 참고안이며 실제 권한 확보·배포·테스트는 하지 않습니다.
유료 리소스 생성과 inference는 사용자의 별도 승인·설정 후 실행합니다.

## 권한 구분

일반 `Contributor`는 리소스 관리 권한입니다.
Entra ID 기반 모델 inference의 `DataActions`와 역할 할당 권한을 혼동하지 않습니다.

| 작업 | 권장 역할 | 범위 |
|------|-----------|------|
| 기존 모델 호출 | Cognitive Services OpenAI User | 각 대상 OpenAI 리소스 |
| 모델 배포 생성·변경 | Cognitive Services OpenAI Contributor | 각 대상 리소스 |
| 계정·네트워크 생성·관리 | Contributor 또는 적합한 서비스 관리 역할 | 필요한 resource group |
| quota 조회 | Cognitive Services Usages Reader | 대상 subscription |
| 역할 할당 | Role Based Access Control Administrator 등 | 관리자가 대상 범위에서 수행 |

개발자 계정과 운영 Agent의 Managed Identity는 다른 principal입니다.
개발자에게 권한이 있어도 Agent identity가 자동으로 권한을 상속하지 않습니다.
운영 identity에는 각 backend resource의 최소 호출 권한을 할당합니다.
단순 호출만 필요하면 Contributor를 요청할 필요가 없습니다.
자세한 역할은 [공식 RBAC 문서](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/role-based-access-control)를 확인하세요.

Bicep 예제는 지정한 principal에 Cognitive Services OpenAI User를 할당합니다.
배포 실행자는 resource 생성 권한뿐 아니라 role assignment 권한도 필요합니다.
Contributor만 있다면 관리자가 역할을 미리 부여하고 template의 역할 생성 부분을 별도로 조정해야 합니다.
Owner를 모든 개발자나 운영 identity에 부여하는 방식은 권장하지 않습니다.

## 리전과 quota 점검

1. Foundry Quota에서 네 모델의 실제 `Scope`, 모델 version, SKU, 승인 TPM/RPM을 기록합니다.
2. `Global`이면 같은 subscription/model/version의 리전별 배포가 quota를 공유함을 확인합니다.
3. 지역 Scope가 남아 있는 모델만 실제 지역별 할당을 확인합니다.
4. region/model availability, 접근 승인, deployment capacity 단위·최소값을 확인합니다.
   `capacity=1`이 모든 모델에서 같은 TPM/RPM을 뜻하지 않습니다.
5. 모델 ID·버전을 pin하고 자동 upgrade를 막습니다. 만료 시 별도 재검증 후 변경합니다.
6. 예상 비용, 데이터 처리 범위, content safety와 네트워크 정책을 검토합니다.

실제 배포 가능한 capacity는 [공식 지역 가용성 표](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability#global-standard)만으로 확인할 수 없습니다.
포털과 해당 구독의 관리 API 상태를 함께 확인합니다.

## 선택적 Bicep 배포

[main.bicep](../infra/main.bicep)은 지정한 각 지역에 OpenAI account를 만들고
네 모델의 Global Standard 배포를 생성합니다.
[예제 parameter](../infra/main.bicepparam)는 문서용 endpoint 허용 IP와 빈 principal UUID를 사용하므로
실제로 사용하기 전에 반드시 교체해야 합니다.
EU는 기본 배포하지 않으며 승인 후 locations에 `swedencentral`을 추가할 수 있습니다.

로컬 인증을 끄고 Entra만 허용합니다. Public endpoint는 명시한 IP allowlist로 제한합니다.
이것은 완전한 private-network production landing zone이 아닙니다.
운영에서는 private endpoint, VNet routing, DNS, gateway 네트워크와 진단 설정을 별도로 구성합니다.
Bicep 0.48.1 이상을 권장합니다. 이전 compiler에서 `BCP081`이 나오면
2026-07-01 resource schema를 실제로 검증하지 못한 것이므로 단순 exit 0만으로 합격 처리하지 않습니다.

```powershell
# 로컬 컴파일만 수행하며 Azure 리소스를 만들지 않습니다.
az bicep build --file infra\main.bicep --outfile artifacts\main.json

# 대상 구독을 명시하고, 준비된 테스트 RG와 편집한 parameter를 사용합니다.
az deployment group what-if --subscription "<subscription-id>" `
  --resource-group "<test-rg>" --parameters infra\main.bicepparam

# 비용·권한·네트워크·모델 용량을 검토한 후에만 수동 실행합니다.
az deployment group create --subscription "<subscription-id>" `
  --resource-group "<test-rg>" --parameters infra\main.bicepparam
```

위 create 명령은 이 프로젝트를 만드는 과정에서 실행하지 않습니다.
실험이 끝나면 실험용으로 생성한 정확한 resource만 별도 변경 승인에 따라 정리합니다.
공용·기존 resource group 전체를 자동 삭제하지 않습니다.

## 기존 배포 설정과 사전 확인

```powershell
uv run python -m foundry_resiliency example --mode same `
  --output configs\single-subscription.local.json
```

설정에서 `base_url`, `resource_id`, `deployment`를 실제 값으로 바꿉니다.
`model`과 `version`은 실제 ARM deployment와 같아야 합니다.
모든 local config와 `.env`는 Git에서 제외됩니다. 키를 JSON에 쓰지 않습니다.
기본 Entra 인증을 권장하며, 테스트 키가 필요하면 `api_key_env`에 환경변수 이름만 지정합니다.

```powershell
az login
uv run python -m foundry_resiliency validate-config `
  --config configs\single-subscription.local.json
uv run python -m foundry_resiliency preflight `
  --config configs\single-subscription.local.json --output artifacts\preflight.json
```

`preflight`는 Azure CLI의 로그인 context로 ARM GET만 수행합니다.
계정 리전·endpoint와 deployment의 SKU·모델 ID·고정 버전·Succeeded 상태를 검증합니다.
subscription UUID가 빈 예시이거나 endpoint가 `replace-`이면 즉시 중단합니다.
모든 backend를 검사하며 quota 여유나 inference data-plane 권한을 검증했다고 주장하지 않습니다.

일반 local 실행에서는 `az login` 계정을, Azure hosting에서는 Managed Identity를 사용하도록
`DefaultAzureCredential`이 동작합니다.
다른 identity 환경변수가 설정되어 있으면 credential 선택이 달라질 수 있으므로
관리 plane과 data plane에 어떤 principal을 쓰는지 운영 환경에서 점검합니다.

## 실제 소량 테스트

```powershell
uv run python -m foundry_resiliency smoke --config configs\single-subscription.local.json `
  --model gpt-6.1-sol --market KR --stream --count 5 --confirm-paid `
  --output artifacts\live-normal.json
uv run python -m foundry_resiliency smoke --config configs\single-subscription.local.json `
  --model gpt-6.1-sol --market KR --stream --count 2 --confirm-paid `
  --fault primary-429 --output artifacts\live-region-fallback.json
```

`--fault`는 해당 endpoint를 실제 호출하기 전에 로컬에서 429를 주입합니다.
나머지 backend는 실제 모델을 호출합니다. 실제 Azure 429를 발생시켰다는 뜻이 아닙니다.
실제 오류율과 fault injection 결과를 분리해서 보고합니다.

혼합 모델을 테스트하려면 승인된 allowlist만 남긴 mixed 설정을 사용합니다.

```powershell
uv run python -m foundry_resiliency smoke --config configs\mixed-model.local.json `
  --model gpt-6.1-sol --market UK --stream --count 2 --confirm-paid `
  --allow-model-fallback --fault same-model-429 --output artifacts\live-mixed.json
```

실제 호출의 요청 수 상한은 20개이며, 각 요청은 설정의 시도 상한을 가집니다.
429를 유도하기 위해 대규모 트래픽을 보내지 않습니다.
실환경 결과가 없으면 로컬 시뮬레이션으로 대체 표기하지 않습니다.

## 장애 대응

| 관측 | 확인 사항 | 대응 |
|------|-----------|------|
| quota 미만 429 | effective limit header, prompt+output 추정, burst, shared capacity | server cooldown 준수, 동시성 감소, 승인 후보 전환 |
| 같은 모델 모든 endpoint의 429 | 동일 quota pool 여부, 서비스 상태 | 제한된 시간 내 실패, queue/비동기 업무로 분리, 승인된 타 모델만 사용 |
| 401/403 | 실제 호출 identity, data role, 네트워크 접근 | 자동 모델 전환 금지, 권한·네트워크 수정 |
| TTFT 상승 | queue, prompt 길이, cache, usage tier, 연결 재사용 | 국가별 지표 분리, 부하 완화, profile 재측정 |
| stream 중단 | 마지막 text 수신, tool 부작용, backend 오류 | 자동 재생 금지, 사용자에게 중단 표시 후 새 요청 |
| 일부 리전 복구 | cooldown 만료, 단일 probe 성공 | 점진적 복귀, warm/cold 및 오류율 관찰 |

quota 조정과 shared-pool protective throttling은 다른 원인입니다.
고객별 한 차례 사례에서 복구 시간을 보장하지 않습니다.

## 향후 다중 구독

[참고 SVG](diagrams/global-multi-subscription.svg)는 같은 Entra tenant의 subscription A/B를 가정합니다.
각 구독의 승인 quota를 확인하고, 운영 identity에 각 target resource의 호출 권한을 부여합니다.
일반 Contributor만으로 Entra inference와 role assignment를 모두 할 수 있다고 가정하지 않습니다.
다른 tenant 간 구성은 별도 identity·보안 설계가 필요하며 현재 범위 밖입니다.

진행을 재개할 때만 다음 예제를 생성해 교체합니다.

```powershell
uv run python -m foundry_resiliency example --mode mixed --subscriptions 2 --include-eu `
  --output configs\future-multi-subscription.local.json
```
