**Nghiên cứu: Shopify / DSPy / GEPA và ReBAC cho RAG với OpenFGA — 09/09/2026**

Hai hướng bổ sung nhau: GEPA nhằm cải thiện chất lượng/chi phí thực hiện tác vụ; OpenFGA kiểm soát dữ liệu mà agent được phép đọc. Với coursework, có thể phát triển thành hai thí nghiệm độc lập, mỗi thí nghiệm có baseline và bằng chứng. Đây là nghiên cứu và thiết kế đề xuất; chưa triển khai hoặc đo kết quả trong repo.

**1. Case Shopify: phần nào đã xác minh?**

| Nhận định | Bằng chứng và giới hạn |
|---|---|
| Shopify dùng DSPy + GEPA ở quy mô lớn | Trang DSPy ghi nhận structured metadata extraction trên các Shopify shops, báo cáo khoảng 550× giảm chi phí. Trang tóm tắt không đủ thông tin để tái lập phép so sánh. [DSPy use cases](https://dspy.ai/community/use-cases/) |
| Talk liên quan đến Shop Intelligence | Meetup công bố Kshetrajna Raghavan từ Shopify trình bày multi-agent ReAct và prompt optimization. Tìm được [video “From One-Shot to Agentic”](https://www.youtube.com/watch?v=bxToahwOVpY), nhưng chưa đọc được transcript gốc qua công cụ nghiên cứu. [Trang sự kiện](https://luma.com/je6ewmkx) |
| Chính xác Qwen phiên bản nào, self-host trên phần cứng nào, baseline nào? | Chưa đủ bằng chứng sơ cấp đã truy cập để xác nhận. Không dùng các thông tin Qwen 32B/72B hay con số 75× trong bản kể lại làm cấu hình đã được Shopify xác nhận. |
| Shopify còn công bố một pipeline khác | Bài Sidekick ngày 05/08/2026 dùng DSPy với GEPA/ACE để hiệu chỉnh judge; sau đó có cải thiện harness, SFT, GRPO và nén prompt. Bài ước tính giảm 96% serving cost cho GraphQL agent. Đây không phải bằng chứng GEPA riêng lẻ tạo ra mức giảm đó, và bài không nêu Qwen. [Shopify Engineering](https://shopify.engineering/sidekicks-continual-learning-loop) |

Vì vậy cách trình bày chắc chắn trong coursework là: “Lấy cảm hứng từ case DSPy + GEPA tại Shopify, chúng tôi đánh giá tối ưu prompt cho Qwen self-hosted trong hệ thống của mình.” Chưa nên viết “tái tạo chính xác kiến trúc Shopify” hoặc lấy hệ số tiết kiệm của họ làm mục tiêu mặc định.

**2. DSPy, Qwen và GEPA làm những việc khác nhau**

- **Qwen:** model thực thi tác vụ. Self-host nghĩa là nhóm tự vận hành endpoint inference; vẫn có chi phí máy, vận hành và công suất nhàn rỗi.
- **DSPy:** mô tả chương trình LLM bằng signatures, modules và metric để có thể đánh giá, tối ưu có hệ thống. [DSPy paper](https://arxiv.org/abs/2310.03714)
- **GEPA:** Genetic-Pareto optimizer; tiến hóa các thành phần văn bản dựa trên feedback từ execution. Trong thí nghiệm prompt, nó thay instructions, không cập nhật weights của Qwen. [GEPA paper](https://arxiv.org/abs/2507.19457)

Liên hệ với genetic algorithm: một cá thể là bộ instructions; fitness đến từ eval; mutation là bản sửa do LLM đề xuất sau khi xem lỗi; selection giữ các ứng viên mạnh trên những ví dụ khác nhau. Có thể dùng merge để kết hợp cải tiến. Điểm đặc trưng là mutation dựa trên lý do thất bại, không chỉ thay chữ ngẫu nhiên. Pareto theo các ví dụ cũng không tự động đồng nghĩa tối ưu trade-off chất lượng/latency/chi phí; phải thiết kế mục tiêu đó. [GEPA API](https://dspy.ai/api/optimizers/GEPA/overview/)

Ví dụ feedback do evaluator của repo có thể tạo:

```text
Task: tìm sản phẩm dưới 500.000 đồng cho người dùng hiện tại.
Failure: generated tool arguments omit max_current_price.
Expected: max_current_price=500000; identity comes from trusted context.
Observed: query mentions the budget but scalar filter is absent.
```

Phản hồi này cung cấp lỗi cụ thể để sửa prompt, thay vì chỉ trả “score=0”.

Nên tách **task model**, **reflection model** và **evaluator**. Task model chạy nhiều rollout; reflection model đề xuất instructions; evaluator tạo score/feedback. Reflection model không mặc nhiên là judge. DSPy hỗ trợ metric trả score cùng feedback và giới hạn ngân sách optimization. [GEPA in depth](https://dspy.ai/diving-deeper/gepa-in-depth/)

**3. Thí nghiệm GEPA đề xuất cho repo**

Tên đề tài: **Trace-guided prompt optimization for a self-hosted recommendation agent**.

Câu hỏi: với cùng Qwen và cùng tool/runtime, prompt tối ưu bằng GEPA có tăng task success trên câu hỏi chưa từng thấy so với prompt ban đầu và prompt chỉnh tay?

Repo có [catalog Qwen3.5-0.8B Q8_0](/Users/KHOAI/anhkhoa/RecSys-MLops/configs/llm-ab/catalog/qwen3.5-0.8b-q8_0.json). Đây là cấu hình trong working tree, không phải xác nhận model đang phục vụ production. Model nhỏ có giới hạn năng lực; prompt optimization phải được đo, không thể hứa sẽ thay thế model lớn.

MVP phù hợp nhất là một module hẹp: chuyển yêu cầu tiếng Việt thành query và filters đúng contract của RAG. Bao gồm giá, brand, tình trạng hàng, diễn đạt mơ hồ và trường hợp cần hỏi lại. Sau khi module này có kết quả, mới thử prompt của Context agent với trace tool thực tế.

Thiết kế do nghiên cứu này đề xuất:

1. Khởi đầu với khoảng 200–300 ví dụ đã kiểm tra nhãn; đây là ngân sách pilot, không bảo đảm đủ lực thống kê. Chia train/dev/test theo nhóm intent, template và hội thoại; giữ nguyên một hội thoại trong một split. Tách paraphrase gần trùng để giảm leakage.
2. Tạo ba arm: B0 prompt hiện tại; B1 chỉnh tay với ngân sách eval ghi rõ; B2 GEPA. Cố định model hash, quantization, generation settings, tools, retrieval snapshot và task inputs.
3. Dùng code chấm schema, giá trị filters, tool order/arguments và item alignment. Dùng judge được đối chiếu với người chấm cho độ đầy đủ/grounding. Policy vi phạm là điều kiện loại; không cho điểm diễn đạt bù cho lỗi quyền truy cập.
4. Feedback phải chỉ ra bước sai trong trace. Nếu chỉ chấm câu trả lời cuối, khó biết lỗi ở chọn tool, tạo arguments, retrieval hay diễn giải.
5. Khóa evaluator trước khi tối ưu task prompt. Nếu nghiên cứu tối ưu judge, thực hiện thành thí nghiệm riêng với nhãn người chấm và test riêng.
6. Sau khi chọn prompt bằng dev, chạy locked test. Báo số ca đúng/tổng, paired confidence interval theo câu hỏi/hội thoại, breakdown intent và độ ổn định qua nhiều lần chạy. Không điều chỉnh tiếp dựa trên test rồi gọi nó là held-out.

| Metric | Cách chứng minh |
|---|---|
| Task success | Đúng yêu cầu và constraints trên held-out cases |
| Tool/argument accuracy | Exact/semantic comparison với oracle; phân loại missing, wrong, extra calls |
| Grounding | Claims được evidence hỗ trợ; human audit cho các câu trả lời khó |
| Repeated-run reliability | Mỗi case chạy 3 lần, báo tỷ lệ đúng cả 3 và tổng tỷ lệ đúng |
| Latency / tokens | p50/p95, input/output tokens, retry/tool-call counts dưới cùng tải |
| Chi phí | Chi phí optimization và inference tách riêng; có fixed serving cost nếu self-host |

Không dùng token count làm tiền tiết kiệm trực tiếp khi CPU/GPU vẫn trả tiền cố định. Với cùng mô hình chi phí và C_base > C_new, điểm hòa vốn xấp xỉ N = C_opt / (C_base − C_new), trong đó chi phí mỗi request đã tính phân bổ hạ tầng. Cần báo giả định tải và utilization.

Hai cách tích hợp: dùng DSPy cho module mới và giữ đúng adapter ở deployment; hoặc dùng [GEPA standalone với custom adapter](https://github.com/gepa-ai/gepa) để đánh giá runtime hiện có. Copy riêng instructions khỏi DSPy có thể đổi hành vi do mất formatting, demos hoặc control flow; phải kiểm thử lại end-to-end.

Pipeline hiện tại [validate_workflow](/Users/KHOAI/anhkhoa/RecSys-MLops/jenkins/python/llm_agent_cd/workflow.py:79) bắt buộc templates/tools/overrides cố định và chỉ nhận thay đổi config/model. Vì vậy nên có offline experiment riêng trước; nếu đưa prompt candidate vào release, cần bổ sung artifact/version và validation phù hợp. Không đổi prompt ngầm trong một arm mang nhãn config-only.

Deliverables: dataset có split/version, evaluator version, prompt trước/sau, optimization trace, bảng test/chi phí, ví dụ sửa được và ví dụ vẫn thất bại. Chỉ dùng dữ liệu tổng hợp/đã xử lý phù hợp nếu reflection endpoint nằm ngoài hệ thống.

**4. “OpenGPA” và ReBAC trong RAG**

Nghiên cứu này hiểu ý định là **OpenFGA**, vì đây là authorization engine có tài liệu chính thức cho ReBAC/RAG. Có website OpenGPA riêng; chưa có căn cứ coi hai tên là cùng một sản phẩm. [OpenFGA](https://openfga.dev/)

ReBAC quyết định quyền từ quan hệ giữa người dùng và tài nguyên. Ví dụ tự thiết kế: Alice thuộc nhóm vận hành shop A; nhóm có quyền đọc folder nội bộ; tài liệu thuộc folder; các chunks thuộc tài liệu. Quyền đọc được suy ra theo chuỗi quan hệ. Bob của shop B không có chuỗi cấp quyền này.

Trong RAG, retrieval đúng ngữ nghĩa vẫn có thể lấy tài liệu người dùng không được đọc. OpenFGA dùng để kiểm tra quyền trước khi nội dung được đưa vào context cho LLM. Nó không thay vector search hay tự cung cấp đăng nhập. [RAG authorization](https://openfga.dev/docs/modeling/agents/rag-authorization)

Mô hình dưới đây là minh họa đề xuất, chưa chạy validation bằng OpenFGA CLI:

```fga
model
  schema 1.1

type user

type group
  relations
    define member: [user]

type folder
  relations
    define viewer: [user, group#member]

type document
  relations
    define parent: [folder]
    define viewer: [user, group#member] or viewer from parent

type chunk
  relations
    define parent: [document]
    define can_read: viewer from parent
```

Các tuples mẫu, theo thứ tự `(user, relation, object)`:

```text
(user:alice, member, group:shop-a-ops)
(group:shop-a-ops#member, viewer, folder:shop-a-internal)
(folder:shop-a-internal, parent, document:refund-policy-a)
(document:refund-policy-a, parent, chunk:refund-policy-a-01)
```

Kỳ vọng `Check(user:alice, can_read, chunk:refund-policy-a-01)` được phép; Bob bị từ chối nếu không có grant khác. Xóa membership sẽ loại bỏ đường cấp quyền này, nhưng direct grant khác vẫn có thể cho phép đọc. Mô hình này minh họa kế thừa quyền, chưa có tenant isolation đầy đủ; service ghi tuples phải chặn liên kết sai tenant. Tham khảo cú pháp [OpenFGA configuration language](https://openfga.dev/docs/configuration-language).

Nếu toàn bộ chunks có cùng ACL với document, MVP có thể check document một lần rồi áp cho các chunks tương ứng; không bắt buộc tạo một tuple cho từng chunk. Cần ánh xạ parent đáng tin và xử lý chunk bị thiếu mapping theo hướng từ chối.

**5. Đặt OpenFGA ở đâu trong hệ thống?**

Ba cách kết hợp search và quyền được tài liệu mô tả: search rồi BatchCheck; lấy allowed IDs bằng ListObjects trước search; hoặc duy trì permission index rồi check lại. Lựa chọn phụ thuộc số tài liệu và tỷ lệ tài liệu user có quyền. Không mặc định ListObjects tốt nhất khi phải liệt kê tập rất lớn. [Search with permissions](https://openfga.dev/docs/interacting/search-with-permissions)

Đề xuất MVP cho repo:

1. Gateway xác thực JWT/session và tạo principal đáng tin. Loại bỏ header do client tự giả mạo; truyền danh tính qua A2A/MCP bằng authenticated service context.
2. Retrieval service dùng tenant scope đã xác thực làm bộ lọc thô, lấy candidate IDs và metadata cần cho authorization. Không dùng tenant/user ID do model tự sinh làm quyền hạn.
3. BatchCheck quyền đọc các document; chỉ hydrate/đưa text được phép tới reranker, compressor, agent và output. Nếu adapter hiện đã hydrate text, phải giữ text trong trusted retrieval boundary và không log/forward trước check.
4. Nếu lọc quyền làm thiếu top-k, overfetch/refill trong một ngân sách hữu hạn. Đo recall trên tập tài liệu được phép; không chỉ lọc 10 candidates thành 1 kết quả rồi kết luận retrieval vẫn tốt.
5. Exact chunk lookup và batch lookup phải kiểm tra cùng policy. Từ chối nội dung/metadata không được phép; tránh lỗi API làm lộ tài liệu có tồn tại.
6. Không xác định được principal, mapping ACL bị thiếu, hoặc authorization service lỗi thì không trả private content. Có thể trả lỗi tạm thời thay vì giả vờ “không có kết quả”.

Các điểm cần thay đổi theo source đã đọc:

| Vị trí | Hiện trạng và việc cần làm |
|---|---|
| [MCP middleware](/Users/KHOAI/anhkhoa/RecSys-MLops/apps/agentic/recsys-feature-rag-mcp/src/recsys_feature_rag_mcp/app.py:76) | Bearer token chung xác thực caller; cần danh tính end-user cho kiểm tra từng resource |
| [A/B router](/Users/KHOAI/anhkhoa/RecSys-MLops/apps/agentic/llm_ab_router/server.py:287) | Đọc x-user-id cho ownership; riêng dòng này chưa chứng minh header đã được xác thực end-to-end |
| [RAG contracts](/Users/KHOAI/anhkhoa/RecSys-MLops/apps/api-serving/rag-api/src/recsys_rag_api/contracts.py:67) | Có chunk_id/item_id/source_key, chưa có document/tenant ACL mapping rõ; cần mapping chuẩn và quản lý theo ingestion lifecycle |
| [Retrieve endpoint](/Users/KHOAI/anhkhoa/RecSys-MLops/apps/api-serving/rag-api/src/recsys_rag_api/app.py:233) | Chưa thấy per-document authorization trong handler; thêm enforcement ở service dùng chung |
| [Exact/batch lookup](/Users/KHOAI/anhkhoa/RecSys-MLops/apps/api-serving/rag-api/src/recsys_rag_api/app.py:248) | Bảo vệ cả hai đường để không bypass semantic retrieval bằng chunk ID |

Đây là nhận xét về đường code đã đọc, không phải khẳng định mọi deployment đang public hoặc không có gateway protection.

User ID của người được recommendation, ví dụ `1001`, là resource subject; không tự động bằng principal của người đang gọi. Cần policy riêng cho profile/features của người dùng nếu những dữ liệu đó là riêng tư. Với agent, scope hiệu lực nên là giao của quyền người dùng và phạm vi task được ủy quyền. [Task-based authorization](https://openfga.dev/docs/modeling/agents/task-based-authorization)

Thu hồi quyền cần xét cả cache và hội thoại. OpenFGA có HIGHER_CONSISTENCY bỏ qua cache của chính engine để đọc database, nhưng không xóa application cache hay context đã đưa vào model. Cần đo độ trễ ACL sync và invalidation riêng. [Query consistency](https://openfga.dev/docs/interacting/consistency)

Đề xuất lưu provenance document IDs cho cached answers, conversation summaries và evidence; reauthorize trước khi tái sử dụng, hủy hoặc tạo lại phần chứa tài liệu đã bị thu hồi. Không thể “thu hồi” thông tin người dùng đã nhìn thấy trước đó. ACL updates nên có event riêng, không phụ thuộc chờ rebuild vector index. Các bước ghi quan hệ/reindex cần giữ tài liệu mới ở trạng thái không đọc được cho tới khi mapping quyền sẵn sàng.

OpenFGA quyết định quyền theo model và tuples mà ứng dụng cung cấp; nó không tự đồng bộ ACL nguồn, không tự chặn prompt injection, cũng không kiểm chứng claim LLM. Auth0 FGA là dịch vụ managed liên quan; lựa chọn self-hosted OpenFGA cần tính thêm vận hành datastore và availability. [OpenFGA repository](https://github.com/openfga/openfga)

**6. Thí nghiệm OpenFGA đề xuất**

Tên đề tài: **Relationship-aware RAG with permission revocation evaluation**.

Nếu corpus chỉ là mô tả sản phẩm công khai, ReBAC ít giá trị thực tế. Demo nên thêm một bộ dữ liệu tổng hợp có lý do phân quyền rõ: hai shop, tài liệu nội bộ, tài liệu chia sẻ theo nhóm và vài catalog pages công khai. Nêu rõ đây là extension thử nghiệm.

| Nhóm test | Kỳ vọng và phép đo |
|---|---|
| Cùng query, khác principal | Alice/Bob nhận đúng tập evidence được phép; nội dung cấm không tới LLM |
| Grant trực tiếp / nhóm / kế thừa | So quyết định với permission oracle, gồm nhiều đường cấp quyền |
| Thu hồi membership hoặc grant | Đo thời gian từ xác nhận cập nhật ACL tới lần đọc bị chặn; xét grant thay thế |
| Exact ID / batch IDs | Không bypass qua chunk lookup; mixed batch không lộ phần bị cấm |
| Giả principal hoặc tenant trong prompt/tool args | Không đổi authenticated principal hay scope |
| Query nhắm tài liệu cấm / injection trong tài liệu | Quyền truy cập không do LLM quyết định; test injection riêng với nội dung được phép |
| Cache và multi-turn | Không tái dùng private evidence đã mất quyền trong turn mới |
| OpenFGA timeout / ACL mapping thiếu | Không có private content lọt qua; availability degradation được ghi nhận |
| Nhiều candidates bị loại | Báo authorized Recall@k, số candidates checked, p95 overhead |

Chỉ số chính: unauthorized-context rate (case có bất kỳ evidence bị cấm tới LLM / tổng case cần chặn), false-denial rate, authorized Recall@k, latency và revocation delay. Mục tiêu test là zero unauthorized-context events; kết quả 0/N chỉ là bằng chứng trong phạm vi N cases, không chứng minh hệ thống không thể rò rỉ.

So sánh baseline không lọc chỉ trên dataset tổng hợp; so thêm tenant-only filtering để chỉ ra lợi ích của chia sẻ theo nhóm/kế thừa. Không bật baseline thiếu authorization trên private traffic thật. Evidence cần chứa trace IDs, policy/model version, principal đã pseudonymize, document IDs và quyết định; không cần log nguyên văn private content.

**7. Ưu tiên cho hai novel ideas trong Sheet3**

Workbook yêu cầu “Document idea + proof it worked!”; bản đọc trước nằm ở [nghiên cứu novel ideas](/Users/KHOAI/anhkhoa/RecSys-MLops/docs/ops/llm-novel-ideas-research-2026-09-07.md). Hai hướng này là ứng dụng/thực nghiệm bổ sung, chưa phải tuyên bố thuật toán mới hoặc bảo đảm điểm.

| Hướng | Hợp khi | Phần khó nhất | Bằng chứng cần có |
|---|---|---|---|
| DSPy + GEPA | Muốn nghiên cứu chất lượng LLM và tận dụng Qwen self-hosted | Dataset và evaluator đáng tin; chi phí rollout | Prompt trước/sau và cải thiện held-out, kèm latency/cost |
| ReBAC + OpenFGA | Muốn RAG có dữ liệu private, group sharing, multi-tenant | Identity propagation, ACL lifecycle và cache/history | Permission matrix, zero observed leaks, revoke demo và overhead |

Khuyến nghị: ưu tiên GEPA nếu giữ nguyên use case catalog công khai. Chọn thêm OpenFGA nếu chủ động mở rộng sang tài liệu shop/người dùng có quyền khác nhau. Khi kết hợp, cố định authorization bằng code; chỉ tối ưu hành vi LLM trên dữ liệu đã được cấp quyền. GEPA không được sửa policy hoặc quyết định principal.
