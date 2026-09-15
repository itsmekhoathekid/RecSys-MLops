# Thiết kế evaluation pipeline với Langfuse cho RecSys-MLops

Ngày nghiên cứu: 09/09/2026. Đây là đề xuất dựa trên working tree và tài liệu chính thức, chưa phải triển khai hoặc benchmark live. Kế thừa phân tích trong `agent-evaluation-research-2026-09-07.md`.

## Quyết định kiến trúc

Jenkins chạy Python Evaluation Runner trong job có giới hạn tài nguyên. Runner lấy dataset đã đóng băng, gọi baseline/candidate qua A2A, thu thập trusted child-task evidence, chấm điểm và xuất quyết định PASS/FAIL/HOLD. Langfuse lưu dataset, experiment, trace, score và hỗ trợ review. Giữ Git/Helm là nguồn cấu hình agent; không bắt buộc chuyển prompt sang Langfuse.

Ba đường tích hợp khác nhau:

- **Execution:** Jenkins → runner → A2A Coordinator → specialist → MCP → services; model calls vẫn đi qua agentgateway.
- **Tracing:** runner và runtime → OTel Collector → Langfuse. Mỗi case/trial có trace riêng và session mới; không đặt cả experiment dưới một trace chung.
- **Evaluation data:** runner ↔ Langfuse Dataset/Score API; runner → JSON/JUnit/evidence artifacts → Jenkins archive hoặc object storage đã có.

Langfuse SDK hỗ trợ custom task/evaluators. Hosted dataset phù hợp để so sánh dataset runs trên UI; local-data execution có khác biệt về khả năng xuất hiện trong dataset-run UI. Với Python, ưu tiên API `dataset.run_experiment(...)`, task nhận `item.input`, evaluator trả `Evaluation`. Pin SDK và kiểm tra API thực tế trước khi triển khai. [Experiments via SDK](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk).

## Những gì tái sử dụng, những gì phải bổ sung

| Hiện trạng repo | Cách tích hợp |
|---|---|
| `jenkins/python/llm_agent_cd/workflow_evidence.py` kiểm tra A2A/trajectory/tool evidence | Tái sử dụng cho contract fixtures; không xem JSON preservation là semantic grounding |
| Evaluator HOLD khi thiếu expected hoặc có nhiều user turns | Bổ sung outcome grader và evidence theo turn trước khi gate natural-language/multi-turn cases |
| `gates.py` yêu cầu đúng 20 functional cases và không replay | Giữ nguyên; bộ quality experiments chạy độc lập, không top-up/replay canary requests |
| RAG verifier và golden queries | Tái sử dụng ở lớp retrieval; tách Recall@k khỏi chất lượng câu trả lời |
| `LLMWorkflowCD.Jenkinsfile` không monitor sau promotion | Online semantic monitoring là phần cần bổ sung; không mặc định job này có monitor 24 giờ |
| `LLMAgentCD.Jenkinsfile` có champion monitoring 24 giờ | Kiểm tra metric thực tế; monitoring hiện có không tự trở thành semantic evaluation |
| Terraform pin chart Langfuse 2.0.1, app 4.17.0 | Đây là phiên bản cấu hình, không phải xác nhận live; SDK có version riêng |

## Dataset và tính tái lập

Giữ reviewed fixtures trong Git, đồng bộ sang dataset Langfuse tên theo content hash, ví dụ `recsys/workflow-quality/<sha>`. Không sửa dataset đó sau khi bắt đầu experiment. Export nội dung và hash để phát hiện drift; baseline/candidate phải dùng cùng snapshot. Không dựa vào private SDK parameters để pin version. Langfuse hỗ trợ input, expected output và metadata trên dataset items. [Datasets](https://langfuse.com/docs/evaluation/experiments/datasets).

Một case cần: case ID, user messages, synthetic user identity, intent, expected outcome, permitted tools/arguments, reference item/chunk IDs, fixture version và criticality. Chỉ gửi user input tới agent; giữ expected outcome và rubric trong runner để tránh lộ đáp án.

Chia bộ test thành contract, natural Vietnamese intent, RAG grounding, composite workflow, missing/empty input, dependency failure, adversarial input và multi-turn. Multi-turn chưa đủ evidence thì báo unsupported/HOLD, không công bố coverage đã đạt.

Release manifest đóng băng Git SHA, image digests, model artifact/checksum, generation parameters, prompt hashes, tool schema, dataset hash, RAG index/feature/ranker snapshots, runner version, judge model/rubric và gate-policy hash. Model name hoặc Git SHA riêng lẻ không đủ tái lập.

Hai chế độ:

1. **Controlled:** agent/model thật, tool fixtures cố định; cô lập tác động prompt/model.
2. **Integration:** A2A/MCP/services thật trong môi trường test, synthetic identity và dữ liệu có version; đo auth, serialization, routing, latency và trace propagation.

Pilot có thể bắt đầu 10 case chạy xuyên hệ thống, sau đó 60 scenario × 3 trials × 2 variants = 360 workflow executions. Đây là lựa chọn khởi đầu, chưa chứng minh statistical power. Giới hạn concurrency; tách cold/warm latency. Không replay production traffic có side effect.

## Graders

| Lớp | Metric đề xuất | Nguồn chấm |
|---|---|---|
| Recommendation | item IDs/order/scores được giữ đúng, đúng user/top_k | Code đối chiếu trusted MCP response |
| Coordinator | chọn specialist, thứ tự phụ thuộc, truyền đúng item IDs | Code từ A2A child-task evidence |
| Context/RAG | nguồn tồn tại, filter đúng, retrieval Recall@k | Code và golden labels |
| Grounding | claim được evidence hỗ trợ, citation đúng claim | Judge với query + answer + actual retrieved evidence |
| Outcome | đáp ứng yêu cầu, không bỏ sót constraints, giải thích phù hợp | Rubric riêng từng intent và human calibration |
| Resilience | timeout/empty/error handling đúng policy | Fault fixtures; phân biệt task success với graceful degradation |
| Efficiency | end-to-end latency, model/tool calls, tokens | Runtime evidence; tránh cộng trùng parent/child usage |

Không dùng LLM judge để quyết định ID bằng nhau hay JSON hợp lệ. Không dùng độ trung thực của Recommendation Agent để thay thế NDCG của BST.

Judge model/rubric phải được pin và kiểm tra trên nhãn human-reviewed trước khi dùng làm gate. Không mặc định model 0.8B đang được đánh giá đủ năng lực tự chấm. Dùng structured scores kèm lý do; coi nội dung agent/tool là dữ liệu không đáng tin, không cấp tools cho judge. Giới hạn chi phí judge độc lập với serving budget.

Langfuse observation evaluators không tự tải sibling/child observations. Muốn đánh giá cả workflow phải ghi bản tóm tắt evidence cần thiết lên logical root hoặc để runner tự tổng hợp rồi chấm. Expected outputs không tự xuất hiện trong mọi production observation. [LLM-as-a-Judge](https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge).

## OTel: các thay đổi bắt buộc trước pilot

Trong `infra/helm/recsys-observability/templates/otel-collector.yaml`:

- Dòng 80 ép `langfuse.environment=production`: cần phân loại trusted evaluation traffic thành `experiment`, không cho public client tự quyết định environment.
- Dòng 109 allowlist hiện bỏ experiment metadata: cần bảo toàn các trường định danh experiment/item và metadata được phép theo SDK pin.
- Các rules redaction input/output cần giữ. Chấm exact IDs từ trusted evidence trước redaction; semantic judge dùng dữ liệu đã kiểm soát/sanitize nhất quán. Không bypass collector để vô tình lưu payload thô.
- Kiểm tra tiếp các processor ở runtime và header forwarding. Có `traceparent` không tự đảm bảo truyền `baggage` hay liên kết đủ child spans.

Theo schema OTel hiện tại, các key chính gồm `langfuse.experiment.id`, `.name`, `.dataset.id`, `.item.id`, `.item.root_observation_id`; root ID bằng span ID của item root. Experiment identity truyền qua baggage; không đặt PII/credentials trong baggage. Score item gắn với root trace ID và observation ID. [OTel experiment ingestion](https://langfuse.com/integrations/native/opentelemetry/experiments).

Chọn một tracing/export path cho mỗi span để tránh duplicate. Dataset và Score API là HTTP API riêng, không đi qua OTLP Collector. SDK tracing phải dùng cấu hình exporter tương thích với pipeline redaction đã chọn, kiểm chứng bằng synthetic trace.

Dùng Vault/ESO cấp Langfuse project credentials cho runner namespace, không nhét chúng vào ModelConfig API key của LLM. Bắt đầu bằng project evaluation riêng nếu cần tách quyền/dữ liệu; environment chỉ là phân loại, không thay thế access control.

## Jenkins gate và bằng chứng

Luồng đề xuất:

**Resolve manifest → Prepare isolated candidate → Contract smoke → Baseline/candidate experiments → Grade → Compare → Archive evidence → Quality gate → Existing canary/promotion.**

Candidate phải truy cập được trực tiếp trước khi có organic traffic. Không thể chỉ thêm một stage trước provision rồi gọi candidate chưa tồn tại. Validator workflow hiện hạn chế các dạng thay đổi; hỗ trợ prompt/topology experiments cần explicit preparation path và validation, không giả định pipeline hiện nhận mọi candidate.

Langfuse có hướng dẫn CI chạy experiment rồi chặn regression; ví dụ chính thức dùng GitHub Actions. Với repo này, dùng Python process/exit status và Jenkins artifacts, không thêm GitHub Actions chỉ để dùng Langfuse. [Experiments in CI/CD](https://langfuse.com/docs/evaluation/experiments/experiments-ci-cd).

Policy đề xuất theo thứ tự:

1. **Evidence completeness:** đủ cases, terminal tasks, required graders và đúng release/dataset IDs. Thiếu → HOLD.
2. **Critical correctness:** observed cross-user access, fabricated item IDs hoặc prohibited tool call → FAIL. Không có vi phạm trong dataset không chứng minh hệ thống an toàn tuyệt đối.
3. **Semantic quality:** floor theo intent và chênh lệch candidate–baseline trong biên cho phép đã hiệu chỉnh bằng pilot. Không lấy một điểm trung bình che regression của nhóm nhỏ.
4. **Reliability/performance:** error, latency và token budget theo policy; tách infrastructure failure khỏi bad answer nhưng cả hai phải hiện trong report.

Judge timeout/invalid JSON/thiếu retrieval evidence là UNKNOWN/HOLD, không bỏ khỏi mẫu số. Kết quả chưa đủ mạnh là inconclusive, không quảng cáo candidate tốt hơn. Repeated trials của cùng scenario không độc lập; nếu tính confidence intervals, resample theo scenario và ghép cặp baseline/candidate.

Blocking graders chạy và hoàn tất trong runner. Gate dùng results/manifest đáng tin của chính job; UI scores dùng để phân tích, không cho một score chỉnh tay không có provenance mở khóa deploy. Async production evaluator không quyết định release đang chờ.

Score API cho phép gắn score với trace/observation và dùng `score_id` để cập nhật idempotently. Thiết kế key từ run/case/trial/grader version; retry upload không chạy lại agent. Flush telemetry và xác nhận upload/report đầy đủ trước khi đóng job. [Scores API/SDK](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk).

Artifacts: `manifest.json`, `case-results.jsonl`, `summary.json`, `junit.xml`, sanitized evidence và Langfuse experiment URLs. Nếu Langfuse unavailable, giữ local artifacts và đánh dấu export incomplete; theo policy MVP, HOLD release đến khi bằng chứng được lưu đầy đủ.

## Feedback loop và kế hoạch thực hiện

Sau deploy: sampled production observations → async scoring → human review → sanitized regression cases → dataset version mới → nightly/next-release experiment. Không tự lấy câu trả lời hiện tại làm ground truth. Không tự động rollback chỉ vì một judge score.

Các bước triển khai đề xuất, chưa được thực hiện:

1. Pin SDK; thử 10 cases; sửa environment/allowlist và xác nhận experiment, item, child traces, scores hiện đúng UI; kiểm tra không duplicate và không lộ dữ liệu.
2. Xây A2A adapter với deadline, terminal-task wait và trusted child evidence; wrapper contract graders hiện có; JSON/JUnit report.
3. Thêm curated dataset, grounding/outcome graders, human calibration và paired comparison. Mở multi-turn sau khi evidence theo turn hoạt động.
4. Tích hợp isolated candidate và mandatory quality gate vào Jenkins; thử một regression cố ý và một trường hợp missing evidence để xác nhận FAIL/HOLD.
5. Thêm production sampling/annotation và nightly regression khi offline pipeline ổn định.

File mới dự kiến: `apps/agentic/evaluation/` cho runner/adapters/graders/reporting; `configs/evaluation/` cho fixtures/rubrics/policy; `jenkins/LLMEvaluation.Jenkinsfile`. Đây là vị trí đề xuất, không phải component đã triển khai. Job có thể dùng namespace CI hiện tại; không cần thêm dịch vụ thường trực hoặc namespace mới chỉ để chạy evaluation.
