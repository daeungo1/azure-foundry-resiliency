---
title: 글로벌 Agent의 동일 모델·혼합 모델 및 다중 구독 아키텍처
description: 설계 경계, routing 순서, quota 장애 도메인과 운영 gateway 확장
ms.date: 2026-10-16
---

## 설계 결정

실행 가능한 Python reference router를 기본으로 하고 APIM과 LiteLLM을 운영 대안으로 제시합니다.
APIM 중심안은 중앙 정책·거버넌스에, LiteLLM 중심안은 여러 공급자 routing에 유리하지만
이 lab에는 필요 이상의 배포·운영 의존성을 추가합니다.

현재 단일 구독에서도 엔드포인트 장애·연결 오류 대응을 테스트할 수 있습니다.
다중 구독은 별도 승인 quota를 사용하는 확장안입니다. 두 가지를 혼동하지 않습니다.
비용이 발생하는 배포, 실제 모델 호출, remote GitHub 게시를 자동 수행하지 않습니다.

## 글로벌 요청 경로

![현재 단일 구독 및 글로벌 진입점](diagrams/global-single-subscription.svg)

현재 범위는 단일 구독입니다. 다중 구독의 실제 구성·권한 요청·테스트는 보류합니다.
[다중 구독 SVG](diagrams/global-multi-subscription.svg)는 향후 참고안으로만 유지합니다.

1. KR, UK, US, CA 사용자를 Front Door 등 전역 진입점에서 인접한 지역 Agent/gateway로 연결합니다.
2. 각 지역에서 인증, tenant/사용자별 admission, 짧고 제한된 대기열을 적용합니다.
3. 사용자 시장의 `region_preferences`와 측정된 TTFT를 기준으로 backend 후보를 선택합니다.
4. 같은 모델·버전의 후보를 먼저 사용하고, 승인한 경우에만 다음 모델 그룹으로 이동합니다.
5. 스트림을 끊김 없이 전달하고 model 변경 여부를 응답 metadata와 감사 로그에 남깁니다.

전역 진입점·지역별 Agent·APIM·공유 admission 저장소는 운영 참고 구성입니다.
이 repo가 실제 제공하는 실행 범위는 Python client/router, 구성 예제, 테스트, 모델 리소스 Bicep입니다.
Front Door와 production Agent hosting을 배포하지 않습니다.

## Case A 동일 모델 우선

![동일 모델 장애 전환](diagrams/same-model.svg)

`gpt-6.1-sol` 요청은 같은 버전의 KR/UK/US/CA 리소스만 사용합니다.
UK 사용자라면 예제 후보는 UK, US, CA, KR 순서입니다.
EU 리소스를 추가하면 UK, EU, US, CA, KR 순서가 됩니다.
다른 세 모델도 동일한 규칙을 사용합니다.

* 429는 server `retry-after-ms`와 `Retry-After` 중 긴 유효 값을 하한으로 지킵니다.
* 408, 500, 502, 503, 504와 연결·timeout 오류만 일시적 오류로 재시도합니다.
* 400, 401, 403, 404, 422 및 501은 자동 model/region fallback하지 않습니다.
* 같은 priority와 시장 선호가 같으면 in-flight와 사용 순서로 분산합니다.
  실제 RTT/TTFT를 자동 학습하는 라우터는 아니며 profile은 측정 후 운영자가 갱신합니다.
* 모든 후보가 막히면 무한 retry 대신 `RoutingExhausted`와 reason을 반환합니다.
* timeout 후 서버 처리가 계속되어 중복 비용이 발생할 수 있습니다. exactly-once를 보장하지 않습니다.

단일 구독의 같은 모델·버전이 subscription-global quota를 공유하면,
resource/region 변경이 quota 초과를 해소하지 못할 수 있습니다.

## Case B 승인된 혼합 모델

![혼합 모델 장애 전환](diagrams/mixed-model.svg)

요청 모델 그룹을 소진한 뒤 `fallbacks`의 순서로 다음 모델 그룹을 사용합니다.
allowlist와 요청 opt-in이 동시에 있어야 합니다.
선택 결과에는 `requested_model`, `selected_model`, `selected_version`, `degraded`가 포함됩니다.
fallback을 원래 모델 성공으로 위장하지 않습니다.

예제의 네 모델 간 전환 순서는 운영 권고 품질 순위가 아닙니다.
업무별 평가셋으로 정답률, hallucination, 안전성, schema, tools 계약, 언어별 품질,
context 크기와 비용을 검증하고 승인된 방향만 남겨야 합니다.
현재 구현은 stateless text-only이므로 tools 등의 기능은 호출 전에 거절합니다.
모델 차이를 숨기기 위해 입력을 잘라내거나 API parameter를 조용히 삭제하지 않습니다.

지역을 바꾸는 경우와 모델을 바꾸는 경우 모두 tenant의 허용된 데이터 처리 범위 안이어야 합니다.
EU 고객의 데이터 처리 제한이 생기면 Global Standard 자체의 적합성을 먼저 재검토합니다.

## 구독과 quota pool

2026-05-07 이후 subscription-level quota 관리가 단계적으로 도입되었습니다.
모델별 적용 여부를 추측하지 말고 Foundry Quota 페이지 `Scope`를 확인합니다.

| 실제 Scope   | 예산 키의 예                                   | 의미 |
|--------------|------------------------------------------------|------|
| Global       | subscription + model + version + GlobalStandard | 구독 내 모든 리전이 quota 공유 |
| Data Zone    | subscription + model + version + data zone      | 이 repo의 Global Standard 범위 밖 |
| 리전 이름    | subscription + region + model + deployment type | 기존 모델별 quota 체계 |

예제의 `quota_pool`은 보수적으로 subscription-global로 묶습니다.
프로세스 내부에서는 같은 key의 headroom을 공유합니다.
실제 Scope가 regional이면 운영 검증 후 region을 포함한 key로 바꾸세요.
다른 구독의 pool에 같은 key를 사용하면 불필요하게 모두 차단될 수 있습니다.
반대로 같은 실제 pool에 서로 다른 key를 주면 quota를 중복 계산할 수 있습니다.

둘 이상의 구독에서는 각각 승인된 quota를 사용할 수 있지만:

* 모든 구독에 동일 모델·고정 버전의 접근 승인과 배포가 있어야 합니다.
* shared resource pool capacity와 지역 장애가 구독별로 독립적이라고 보장하지 않습니다.
* usage-tier latency 기준은 같은 tenant의 모델별 전체 소비량을 합산할 수 있습니다.
* 추가 구독은 서비스 약관과 조직 거버넌스 아래 정당하게 승인·할당된 용도로 사용합니다.
* 쿼터 숫자를 더한 값은 보장 처리량 또는 보장 TTFT가 아닙니다.

## Admission과 복구 상태

헤더의 remaining requests/tokens를 읽고 입력 추정치 + 최대 output budget을 선예약합니다.
동일 창에서 뒤늦게 도착한 큰 remaining 값으로 예산을 다시 늘리지 않습니다.
reset header는 숫자 초와 `250ms`, `1m30s`를 처리합니다.
remaining만 있고 유효한 reset이 없으면 경고 후 60초간 보수적으로 유지합니다.
헤더가 아예 없으면 관측 예산이 없으므로 endpoint concurrency와 실제 오류를 기준으로 제한합니다.

429/일시 오류는 endpoint별 cooldown에 반영합니다.
임계 횟수에 도달하면 circuit을 열고, 만료 후 하나의 half-open probe만 허용합니다.
서버의 긴 retry 지시를 짧은 로컬 cap으로 줄이지 않습니다.
대기 시간이 deadline을 넘으면 그 자리에서 명시적으로 실패합니다.

`max_attempts`는 모든 리전·모델을 합한 총 시도 수입니다.
후보 수보다 작게 설정하면 fallback 그룹까지 도달하지 않을 수 있습니다.
혼합 예제는 8회, 동일 모델 예제는 4회입니다.
각 attempt timeout과 전체 첫 응답 deadline은 별도 예산입니다.

## 스트리밍과 Agent 상태

첫 non-empty `response.output_text.delta` 전까지는 장애 전환할 수 있습니다.
`response.created`, keepalive, 빈 chunk를 첫 토큰으로 세지 않습니다.
첫 텍스트 이후 disconnect, refusal, incomplete는 호출자에게 전달하고 자동 재생하지 않습니다.
stream context가 닫힐 때까지 backend concurrency slot을 유지합니다.

Agent session/history/tool 결과는 호출자 소유의 저장소에 유지해야 합니다.
이전 endpoint의 response ID나 서버측 conversation을 다른 endpoint에 전달할 수 있다고 가정하지 않습니다.
스트림이 끊기면 사용자에게 중단을 표시하고 새 요청으로 명시적으로 재개합니다.
외부 tool 실행에는 별도의 idempotency key와 중복 실행 방지가 필요합니다.

## APIM 운영 확장

별도 APIM instance를 지역별로 두거나 지원되는 multi-region tier를 선택합니다.
Developer tier를 production HA로 간주하지 않습니다.
backend pool은 모델·버전 단위로 분리하고, `429` circuit에 `acceptRetryAfter`를 설정합니다.
Entra 인증, token limit, private networking과 diagnostics를 연결합니다.
지원되는 정책·SKU는 [공식 backend 문서](https://learn.microsoft.com/azure/api-management/backends)를 확인합니다.

APIM circuit/load-balancing 상태는 gateway instance 간 완전 동기화되지 않습니다.
Python replica의 상태도 공유되지 않습니다.
운영에서는 실제 quota scope에 맞는 공유 예산, 사용자별 공정성, bounded ingress queue를 구현하고,
APIM과 SDK와 application이 각각 retry하여 호출 수가 곱해지지 않도록 소유 계층을 하나로 정합니다.

## 합격 기준과 미구현 범위

로컬 합격 기준은 동일 모델 불변식, 승인된 fallback, 시도 상한, 긴 cooldown 존중,
terminal 오류 fail-fast, 헤더 선제 제한, half-open 복구, stream 이후 재생 금지입니다.
실제 RTO, 성공률, TTFT 목표는 실환경 테스트에서 별도로 합의·측정합니다.
분산 quota coordinator, gateway/Agent hosting, 실제 부하 생성, 모델 품질 평가 자동화는 미구현입니다.
