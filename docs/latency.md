---
title: 익명 지역별 글로벌 Agent TTFT 설계
description: TTFT 예산, streaming, 지역 측정과 Agent end-to-end 지연 최적화
ms.date: 2026-10-19
---

## 무엇을 측정할 것인가

사용자가 보는 첫 유용한 토큰의 시간은 다음을 모두 포함합니다.

```text
User TTFT =
  사용자 -> Edge/지역 Agent 네트워크
  + 인증/검색/도구/프롬프트 준비
  + admission 대기와 gateway
  + Foundry 왕복/대기/추론 첫 텍스트 생성
  + 사용자까지의 stream 전달
```

CLI의 `first_token_ms`는 Python 라우터 호출 시작부터 SDK의 첫 non-empty text delta까지입니다.
국가별 사용자 last-mile이나 실제 Agent 검색·tool latency를 포함하지 않습니다.
Global Standard의 리소스를 사용자 가까이에 놓아도 실제 추론 리전과 TTFT는 보장되지 않습니다.
`simulate` 결과는 별도입니다. 모의 정책 시계를 사용하므로 결과의 `first_token_ms`를
실환경 TTFT와 비교하면 안 됩니다. 실제 TTFT 임계치 평가는 `smoke --stream --ttft-budget-ms`로
승인된 실제 배포에 대해 수행해야 하며 현재 공유 테스트에서는 실행하지 않았습니다.

Azure Monitor에서는 절대 TTFT에 가까운 `AzureOpenAITimeToResponse`를 사용합니다.
`AzureOpenAINormalizedTTFTInMS`는 prompt token 수로 정규화된 지표이므로
절대 TTFT 목표와 비교하지 않습니다. TBT와 TTLT도 함께 봐야 합니다.

## 시장별 초기 선호

| 사용자 프로필 | 첫 후보  | 다음 후보          | 비고                            |
|---------------|----------|--------------------|---------------------------------|
| Region01      | Region01 | Region03, Region04 | 승인된 인접 후보와 실측 비교     |
| Region02      | Region02 | Region05, Region03 | Region05는 향후 확장             |
| Region03      | Region03 | Region04, Region02 | 실제 사용자 분포 기준 재측정     |
| Region04      | Region04 | Region03, Region02 | 다른 승인 후보와 비교 후 결정    |
| Region05      | Region05 | Region02, Region03 | 향후 추가, 데이터 처리 승인 우선 |

이는 익명화된 구성 예시이지 실측 latency ranking이 아닙니다.
실제 국가·Azure location과 별칭의 매핑은 비공개 운영 설정에서 관리합니다.
예제는 Region01~Region04를 사용하며 `--include-expansion`으로 Region05를 추가합니다.
각 시장에서 국가·ISP·업무·모델별 p50/p95/p99를 실측한 후 routing profile을 바꿉니다.
지역 preference는 데이터 거버넌스 allowlist를 대신하지 않습니다.

## TTFT를 낮추는 실행 포인트

1. 사용자 인접 Edge/Agent/gateway를 사용하고 불필요한 대륙 간 hop을 제거합니다.
   gateway를 Region03 한 곳에 두고 Region01 사용자가 항상 왕복하게 만들지 않습니다.
2. SDK client와 HTTPS connection pool을 재사용합니다. Managed Identity token도 SDK의
   credential cache를 사용하고 매 요청 CLI 로그인이나 client 재생성을 하지 않습니다.
3. streaming을 켜고 proxy/ingress/클라이언트의 response buffering을 끕니다.
   SSE keepalive와 idle timeout을 점검하고 완료 후 한 번에 출력하지 않습니다.
4. 긴 system prompt, 대화 이력, RAG 문서를 줄입니다. 필요한 데이터만 검색하고
   서로 독립적인 검색·조회는 병렬화합니다. 요약 때문에 필요한 사실을 잃지 않도록 평가합니다.
5. 재사용되는 정적 prompt prefix를 앞에 안정적으로 배치하여 지원되는 prompt caching을 활용합니다.
   지역·모델 전환에서 cache locality가 사라질 수 있으므로 warm/cold를 나눠 측정합니다.
6. 업무 품질을 만족하는 모델과 지원 parameter를 선택합니다. 모델별 지원이 확인된 경우에만
   reasoning effort 등 지연 관련 옵션을 조정합니다. 이 text adapter는 해당 옵션을 임의 전송하지 않습니다.
7. 큰 output 상한을 불필요하게 요청하지 않습니다. 이는 quota 선점과 TTLT에 영향을 주며,
   output 상한 축소만으로 TTFT가 반드시 줄어드는 것은 아닙니다.
8. 동시성·대기열을 제한하여 긴 queue를 만들지 않습니다. 부하를 천천히 올리고
   429, headroom, TTFT, TBT, 오류와 비용을 함께 봅니다.
9. 재시도는 짧고 제한된 budget 내에서만 합니다. 한 endpoint에서 긴 backoff로 기다리기보다
   즉시 사용할 수 있는 승인 후보를 선택합니다. server Retry-After를 무시하지 않습니다.
10. 동일 요청을 여러 지역에 무조건 복제하는 hedging은 기본 비활성화합니다.
    중복 과금, quota 소모, 서버 잔여 실행, Agent tool 중복 부작용이 있습니다.

## 시간 예산

예제의 첫 응답 deadline은 8초, stream attempt의 첫 텍스트 제한은 2초입니다.
전체 스트림 context에는 별도의 60초 제한이 있습니다.
이 값들은 구성 시연용이며 고객 SLO나 서비스 성능 보장이 아닙니다.

2초 first-token budget이 모델 특성보다 짧으면 멀쩡한 backend를 계속 timeout 처리할 수 있습니다.
특히 복잡한 추론 업무는 단순 짧은 응답과 다른 기준을 사용해야 합니다.
업무군별 목표를 정한 뒤 국가별 warm/cold baseline을 보고 예산을 정합니다.
마지막 fallback에 남은 시간이 없으면 추가 요청을 보내지 않습니다.
한 번 첫 텍스트를 노출한 stream은 재시도하지 않습니다.

## 실제 측정 절차

1. Region01~Region04의 실제 Agent hosting 위치와 사용자 접점에 probe를 둡니다.
   개발자 PC 한 대의 `--market` 변경은 국가별 측정이 아닙니다.
2. 동일 prompt 길이, output 예산, 모델·버전과 동시성으로 비교합니다.
   cold-start, warm connection, prompt-cache warm/cold를 나눕니다.
3. 아래 smoke를 소량 실행하고 실제 측정 harness에서 충분한 표본을 수집합니다.
4. 정상, 특정 endpoint 오류, quota 압력, 승인 fallback을 나눠 결과를 비교합니다.
5. 실제 Global Standard capacity를 일부러 고갈시키지 않습니다.
   모의 fault와 실제 service 429를 보고서에서 분리합니다.

```powershell
uv run python -m foundry_resiliency smoke --config configs\single-subscription.local.json `
  --model gpt-6-luna --market Region02 --stream --count 10 --confirm-paid `
  --ttft-budget-ms 2000 --output artifacts\live-region02-luna.json
```

`2000`은 사용자가 지정한 실험 임계치의 예이며 보장값이 아닙니다.
성공 stream의 관측 p95가 임계치를 초과하거나 요청이 실패하면 exit code가 1입니다.
표본 10개의 p95는 거의 최대값인 기술 통계일 뿐 안정적인 production percentile 추정이 아닙니다.
실패 요청을 제거한 성공 TTFT만 보면 생존자 편향이 생기므로 실패율과 deadline 위반을 반드시 함께 보고합니다.

## 다중 구독의 영향

별도 구독 quota는 admission 여유를 줄 수 있으나 inference 성능을 예약하지 않습니다.
같은 tenant의 usage-tier latency 기준은 모든 구독·리전의 모델 소비량을 합산할 수 있습니다.
따라서 “구독 2개면 TTFT도 절반” 또는 “동일 모델 capacity가 완전히 독립”이라고 설명하면 안 됩니다.
[공식 latency 가이드](https://learn.microsoft.com/azure/foundry/openai/how-to/latency)와
[usage-tier 기준](https://learn.microsoft.com/azure/foundry/openai/quotas-limits#usage-tiers)을 함께 확인합니다.
