# Reviewer 2 독립 리뷰

## 검토 대상

- 논문: *State-Grounded Conversational Retrieval for Interactive Analytical Dashboards*
- 원고: [`paper/6a549455b0d9b08aa493b750/00_main.pdf`](../../paper/6a549455b0d9b08aa493b750/00_main.pdf)
- 심사 관점: SIGIR-AP

## 논문 요약

본 논문은 대화와 대시보드 상태를 함께 사용하는 규칙 기반 hybrid ranker와 AER 표현을 제안한다. 19개 과제에서 구성한 57개 query-session에서 R@1 0.9123을 보고하고, 30개 과제의 evidence 재현성, browser-agent 진단 및 5명 사용자 연구를 보조 평가로 제시한다.

## 강점

1. **AER와 relevance 정의가 분석적으로 유용하다.**

   record relevance와 measure·dimension·filter·hierarchy 수준의 component relevance를 구분한 것은 검색 오류를 세밀하게 진단할 수 있는 좋은 출발점이다.

2. **평가 결과의 적용 범위를 솔직하게 제한한다.**

   Table 3의 evidence coverage를 retrieval effectiveness로 해석하지 않고, Table 6도 causal comparison이 아니라 downstream diagnostic이라고 명시한다. 과도한 주장을 피하려는 태도가 좋다.

3. **질의 유형별 결과가 상태 정보의 잠재적 가치를 선명하게 보여준다.**

   Explicit 조건에서는 세 방법 모두 R@1 0.9474이지만 elliptical 조건에서는 BM25 계열이 0이고 dialogue+state가 0.8947이다. 적어도 구성된 stress test 내에서는 상태 정보의 필요성이 명확하다.

## 약점 및 개선안

1. **state와 gold label 사이에 순환성이 존재할 수 있다.**

   gold annotation에는 primary chart뿐 아니라 active tab, filter, interaction type 및 supporting chart가 포함되고, 제안 방법은 focused/linked chart를 우선한다. 결과적으로 입력 state가 정답 chart를 사실상 직접 지정할 가능성이 있다. 후보가 단일 대시보드의 차트 10개뿐이라는 점도 과제를 쉽게 만든다.

   **개선안:** state 생성과 relevance 판정을 독립적으로 수행해야 한다. 실제 사용자가 만든 noisy interaction trace를 사용하고 여러 annotator의 relevance 판단과 agreement를 보고해야 한다. 후보 수와 대시보드 수를 확장하고 bootstrap confidence interval 및 paired significance test도 추가해야 한다.

2. **비교군이 약해 신규성을 판단하기 어렵다.**

   현재 비교군은 query-only BM25와 단순 history concatenation뿐이다. 따라서 AER의 효과인지 structured state feature를 추가한 일반적인 휴리스틱의 효과인지 구분하기 어렵다. Related Work 역시 최신 session search, multimodal interaction retrieval 및 analytical provenance 연구와의 직접 비교가 부족하다.

   **개선안:** BM25+structured filtering, state serialization, conversational query rewriting, dense retriever, learning-to-rank 및 LLM reranker를 포함해야 한다. oracle-state와 noisy-state를 함께 비교하면 제안 방법의 상한과 실제 강건성을 구분할 수 있다.

3. **재현성과 시스템 비용 분석이 부족하다.**

   가중치, tie-breaking, 인덱싱 문서 형태, AER 갱신 및 invalidation 절차가 불명확하다. 상태 기반 검색은 실시간 대시보드에서 계속 갱신되어야 하지만 latency, storage, token/tool 비용도 보고되지 않는다.

   **개선안:** 실행 가능한 artifact와 함께 ranking 설정 전체를 공개하고 indexing/query latency, AER 생성·갱신 비용 및 end-to-end token/tool cost를 보고해야 한다. 동일 환경의 no-state/state-aware 반복 실행과 실패 유형별 분석도 필요하다.

## 잠정 종합평가

- **평가:** Weak Reject
- **확신도:** 4/5

문제 설정과 AER 관점은 SIGIR-AP에 잘 맞고 논문의 자기 제한도 신중하다. 그러나 현재 핵심 성능 향상은 gold annotation에서 결정론적으로 생성된 작은 상태·질의 집합에 의존하며, state 신호가 정답 chart를 사실상 직접 지정하는 구조를 배제하기 어렵다. 또한 matched end-to-end 비교와 경쟁력 있는 IR baseline이 없어 아이디어의 흥미로움이 채택에 필요한 실증적 설득력으로 이어지지 못했다.
