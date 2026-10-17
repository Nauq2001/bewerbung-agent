# Kiến trúc: Job Search Platform

Kế hoạch dựng sản phẩm web đa người dùng từ workspace Claude Code hiện tại.

Tài liệu này mô tả **cái sẽ xây**, không mô tả cái đang có. Phần `.claude/` và `.agents/`
của repo hiện tại là nguồn tham khảo domain logic, xem mục 8.

---

## 0. Phạm vi và ràng buộc

| Hạng mục | Quyết định | Hệ quả kiến trúc |
|---|---|---|
| Người dùng | **Chủ sở hữu dùng trước**, sau đó mở cho vài người quen | Giai đoạn 1–2 chạy một user, không cần auth. Auth và tách dữ liệu vào Giai đoạn 4 |
| Stack | FastAPI (Python) + React (TypeScript) | Scraper TypeScript gọi qua subprocess, mục 3.4 |
| Database | **Supabase** (Postgres quản lý) | Dùng luôn auth, storage, pgvector, RLS. Mục 2.1 |
| Nguồn dữ liệu | **Nguồn cá nhân trước** (LinkedIn, StepStone, Indeed), API công khai bổ sung | Đảo ngược ưu tiên cũ. Ranh giới tier vẫn giữ nguyên, mục 3 |
| Nhà cung cấp LLM | **Không khoá vào Anthropic** | Lớp provider trừu tượng, mục 4.0. Cấu hình model là **dữ liệu**, không phải code |
| Thứ tự dựng | Backend + FastAPI test được trước, rồi frontend | Mục 9 |

**Không nằm trong phạm vi:** đăng ký công khai, thanh toán, ứng dụng mobile, tự động
nộp hồ sơ vào hệ thống của nhà tuyển dụng.

### 0.1 Năm agent

| # | Agent | Việc | Kích hoạt bởi |
|---|---|---|---|
| 1 | CV Reader | Tài liệu cá nhân → hồ sơ có cấu trúc | Người dùng tải file lên |
| 2 | Job Finder | Giữ kho tin luôn mới | Cron |
| 3 | Evaluator | Chấm tin so với hồ sơ | Sau ingest |
| 4 | Document Writer | CV + Anschreiben nhắm vào một tin | Người dùng bấm ứng tuyển |
| 5 | **Interview Coach** | Nghiên cứu công ty, dựng câu hỏi tình huống, luyện tập | Tracker đánh dấu `interview` |

---

## 1. Tổng quan hệ thống

```
┌──────────────────────────────────────────────────────────────┐
│  React SPA (Vite + TS)                                       │
│  Onboarding · Nguồn dữ liệu · Danh sách việc · Chi tiết      │
│  đánh giá · Hồ sơ ứng tuyển · Cài đặt                        │
└───────────────────────────┬──────────────────────────────────┘
                            │ REST + SSE
┌───────────────────────────▼──────────────────────────────────┐
│  FastAPI                                                     │
│  /auth  /profile  /sources  /jobs  /evaluations  /documents  │
└───┬──────────────────┬──────────────────┬────────────────────┘
    │                  │                  │
    ▼                  ▼                  ▼
┌────────┐   ┌──────────────────┐   ┌──────────────────┐
│Postgres│   │ Worker (arq)     │   │ Agent layer      │
│        │   │ · ingest定期     │   │ 4 agent, mục 4   │
│Object  │   │ · evaluate batch │   │ Anthropic SDK    │
│storage │   │ · render LaTeX   │   └──────────────────┘
└────────┘   └────┬─────────────┘
                  │
         ┌────────▼─────────┐
         │ Source connectors│  mục 3
         │ public │ personal│
         └──────────────────┘
```

Nguyên tắc: **API không bao giờ gọi model trực tiếp trong request path.** Mọi lệnh gọi
LLM đều đi qua worker, kết quả ghi vào DB, frontend đọc từ DB hoặc theo dõi qua SSE.
Lý do: lệnh gọi model mất từ vài giây tới vài phút, và request HTTP không nên chờ.

---

## 2. Mô hình dữ liệu

### 2.1 Supabase: dùng gì, bỏ gì

Supabase là Postgres quản lý kèm vài dịch vụ. Dùng có chọn lọc:

| Dịch vụ Supabase | Dùng? | Lý do |
|---|---|---|
| Postgres | **Có** | Đây là lý do chính |
| `pgvector` | **Có** | Có sẵn, không phải tự cài. Phục vụ mục 11 |
| Storage | **Có** | CV, bằng cấp, PDF sinh ra. Thay object storage riêng |
| Auth | **Có, từ Giai đoạn 4** | Khỏi tự viết JWT và luồng mời. `auth.users` thay bảng `users` bên dưới |
| Row Level Security | **Có, từ Giai đoạn 4** | Mục 2.2 |
| Edge Functions | **Không** | Worker nền chạy Python, không chạy Deno. Agent cần LaTeX và subprocess |
| Realtime | **Không** | Đã có SSE từ FastAPI. Thêm một kênh nữa là thừa |

Hệ quả: FastAPI nối tới Postgres của Supabase bằng connection string thường
(SQLAlchemy/asyncpg), **không qua SDK `supabase-py`**. SDK đó tiện cho frontend gọi
trực tiếp, nhưng backend cần transaction, migration và query phức tạp.

Giai đoạn 1–2 chạy một người dùng nên bảng `users` bên dưới cứ tạo bình thường với
một hàng duy nhất. Giai đoạn 4 đổi sang `auth.users` của Supabase: `users.id` giữ
nguyên kiểu UUID nên khoá ngoại không phải sửa.

### 2.2 Row Level Security (Giai đoạn 4)

Khi có người thứ hai, bật RLS trên **mọi** bảng có `user_id`:

```sql
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
CREATE POLICY own_rows ON profiles
    USING (user_id = auth.uid());
```

Bảng `jobs` là ngoại lệ có chủ ý: tin tuyển dụng dùng chung, không gắn `user_id`.
Cái riêng tư nằm ở `evaluations` và `job_user_state` — đó là nơi phải có policy.

### 2.3 Lược đồ

```sql
-- Người dùng và hồ sơ ------------------------------------------------

CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT UNIQUE NOT NULL,
    password_hash   TEXT,                  -- NULL khi đã chuyển sang Supabase Auth
    is_owner        BOOLEAN NOT NULL DEFAULT false,   -- mở tier personal, mục 3.1
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ            -- soft delete, mục 9.4
);

-- File gốc người dùng tải lên (CV, bằng cấp, chứng chỉ)
CREATE TABLE documents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,          -- cv | diploma | certificate | reference
    filename        TEXT NOT NULL,
    storage_key     TEXT NOT NULL,          -- khoá trong object storage
    content_hash    TEXT NOT NULL,          -- tránh xử lý lại cùng một file
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Kết quả của Agent 1. Một bản ghi cho mỗi lần đọc CV.
CREATE TABLE profiles (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_doc_id   UUID REFERENCES documents(id),
    data            JSONB NOT NULL,         -- schema ở mục 4.1
    user_edited     BOOLEAN NOT NULL DEFAULT false,
    model_used      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Tiêu chí tìm việc người dùng tự cấu hình
CREATE TABLE search_prefs (
    user_id         UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    titles          TEXT[] NOT NULL,        -- ['Praktikum Softwareentwicklung', ...]
    home_lat        NUMERIC(9,6),
    home_lon        NUMERIC(9,6),
    max_commute_km  INTEGER,
    contract_types  TEXT[],                 -- ['internship','working_student']
    min_hours_week  INTEGER,
    required_paid   BOOLEAN NOT NULL DEFAULT true,
    languages       JSONB NOT NULL,         -- [{"code":"de","level":"C1"}, ...]
    extra_rules     JSONB                   -- quy tắc riêng, ví dụ 360h certificate
);

-- Tin tuyển dụng -----------------------------------------------------

CREATE TABLE sources (
    id              TEXT PRIMARY KEY,       -- 'arbeitnow', 'greenhouse', 'linkedin'
    tier            TEXT NOT NULL,          -- 'public' | 'personal'   mục 3
    enabled         BOOLEAN NOT NULL DEFAULT true,
    last_run_at     TIMESTAMPTZ,
    last_status     TEXT                    -- healthy | degraded | broken | ratelimited
);

CREATE TABLE jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dedup_key       TEXT UNIQUE NOT NULL,   -- mục 7.1, port từ tools/job_key.py
    source_id       TEXT NOT NULL REFERENCES sources(id),
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    location        TEXT,
    lat             NUMERIC(9,6),           -- geocode khi ingest, để lọc bằng SQL
    lon             NUMERIC(9,6),
    url             TEXT NOT NULL,
    description     TEXT,                   -- toàn văn, cần cho Agent 3
    contract_type   TEXT,                   -- suy ra khi ingest, không dùng LLM
    hours_per_week  INTEGER,
    posted_date     DATE,
    deadline        DATE,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked    TIMESTAMPTZ
);

CREATE INDEX jobs_geo    ON jobs (lat, lon);
CREATE INDEX jobs_recent ON jobs (posted_date DESC) WHERE is_active;

-- Kết quả Agent 3. Theo từng cặp (user, job) vì điểm phụ thuộc hồ sơ.
CREATE TABLE evaluations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id          UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    profile_id      UUID NOT NULL REFERENCES profiles(id),
    scores          JSONB NOT NULL,         -- schema ở mục 4.3
    overall         SMALLINT NOT NULL,
    verdict         TEXT NOT NULL,
    gates           JSONB NOT NULL,         -- eligibility/language/location/contract
    strengths       TEXT[],
    gaps            TEXT[],
    model_used      TEXT NOT NULL,
    evaluated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, job_id, profile_id)    -- hồ sơ đổi thì chấm lại
);

-- Vòng đời ứng tuyển -------------------------------------------------

CREATE TABLE applications (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id          UUID NOT NULL REFERENCES jobs(id),
    folder_slug     TEXT NOT NULL,          -- '<company>_<role>', mục 7.1
    status          TEXT NOT NULL,          -- draft|submitted|interview|offer|rejected|withdrawn
    submitted_at    TIMESTAMPTZ,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, job_id)
);

-- Giấy tờ Agent 4 sinh ra
CREATE TABLE artifacts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id  UUID NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,          -- cv_tex|cv_pdf|letter_tex|letter_pdf|posting_md
    storage_key     TEXT NOT NULL,
    model_used      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Theo dõi chi phí, cần cho mục 4.5
CREATE TABLE llm_calls (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             UUID REFERENCES users(id) ON DELETE SET NULL,
    agent               TEXT NOT NULL,      -- cv_reader|job_finder|evaluator|writer
    model               TEXT NOT NULL,
    input_tokens        INTEGER NOT NULL,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL,
    batch               BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Hai điểm thiết kế đáng giải thích:

**`jobs` tách khỏi `evaluations`.** Một tin tuyển dụng chỉ lưu một lần dù mười người
dùng cùng thấy nó. Điểm đánh giá mới là thứ riêng từng người. Cách này tiết kiệm cả
dung lượng lẫn lệnh gọi ingest, và cho phép sau này thêm "người khác cũng đang xem tin này".

**`lat`/`lon` geocode ngay khi ingest.** Lọc khoảng cách phải là `WHERE` trong SQL, không
phải việc của LLM. Trong phiên làm việc thủ công, bước lọc 43 tin xuống 36 tin theo
khoảng cách tốn 0 token — giữ nguyên tính chất đó.

---

## 3. Lớp nguồn dữ liệu

Đây là phần rủi ro cao nhất của sản phẩm và là nơi quyết định "API + scraper cá nhân"
phải thể hiện thành code.

### 3.1 Hai tier, quyền hạn khác nhau

```python
class SourceTier(StrEnum):
    PUBLIC   = "public"    # API có điều khoản cho phép dùng
    PERSONAL = "personal"  # chỉ chủ sở hữu instance được bật
```

| Tier | Nguồn | Ai dùng được | Ghi chú |
|---|---|---|---|
| `personal` | LinkedIn, StepStone, Indeed, và mọi scraper không có điều khoản cho phép | **Chỉ user có cờ `is_owner`** | CLI LinkedIn gốc ghi rõ "Personal use only (LinkedIn ToS)" |
| `public` | Arbeitnow (feed mở), Adzuna (có key, **kiểm tra điều kiện thương mại**), ATS công khai: Greenhouse, Personio, SmartRecruiters, Workday, Join | Mọi user | Thứ duy nhất mở được cho người quen |

**Ưu tiên xây tier `personal` trước** vì đó là nơi có tin bạn thật sự cần. Nhưng ranh
giới tier phải tồn tại **từ dòng code đầu tiên**, không phải thêm vào ở Giai đoạn 4 —
bởi vì nếu bạn xây một hệ thống coi mọi nguồn như nhau rồi mới tách, thì ngày mời
người quen vào bạn sẽ chạy scraper LinkedIn hộ họ bằng server của mình.

Thi hành ở tầng query, không phải ở tầng UI:

```python
def enabled_sources(user: User) -> list[Source]:
    q = select(Source).where(Source.enabled)
    if not user.is_owner:
        q = q.where(Source.tier == SourceTier.PUBLIC)
    return session.scalars(q).all()
```

Lý do đặt ở đây: nếu kiểm tra ở frontend thì một lỗi nhỏ sẽ khiến bạn chạy scraper
hộ người khác, và đó chính là ranh giới pháp lý bạn muốn giữ.

### 3.1b Khả năng làm được từng nguồn — đánh giá thật

Đây là nơi dễ thất vọng nhất, nên nói trước:

| Nguồn | Độ khó | Thực tế |
|---|---|---|
| **LinkedIn** | Đã xong | CLI có sẵn trong `.agents/skills/linkedin-search/`, đọc trang tin công khai, không cần đăng nhập. Đã kiểm chứng: 43 kết quả cho vùng Rhein-Main. Chỉ cần bọc lại |
| **ATS công khai** | Dễ | Greenhouse, Personio, SmartRecruiters, Join đều có endpoint JSON công khai, không chặn, không đổi cấu trúc thường xuyên. Nhiều công ty Đức dùng |
| Arbeitnow | Dễ | Feed mở, không cần key |
| Adzuna | Dễ | API chính thức, tự đăng ký. Đọc điều kiện thương mại |
| **StepStone** | **Khó** | Chưa có CLI, phải tự viết. Có bảo vệ chống bot. Dự kiến hỏng định kỳ và phải sửa |
| **Indeed** | **Khó nhất** | API cho publisher đã đóng. Bot detection mạnh (tầng Cloudflare). Trong repo gốc, CLI `jobbank` đã có nhánh xử lý riêng cho "Cloudflare blocking" — tiền lệ cho thấy chuyện này xảy ra thật |

Hệ quả thiết kế: **StepStone và Indeed không được là điều kiện để sản phẩm chạy.**
Dựng chúng như connector có thể hỏng, với `health()` báo trạng thái rõ ràng, và luôn
có đường lùi là WebSearch theo cú pháp `site:` (các mẫu truy vấn đã có trong
`job-scraper/search-queries.md`). Một nguồn hỏng chỉ làm giảm số tin, không làm sập
luồng ingest.

Hai quy tắc giữ nguyên từ repo gốc, áp dụng cho mọi connector tier `personal`:

1. **Chạy `robots_check.py` trước khi fetch.** Logic là fail closed: không đọc được
   `robots.txt` thì coi như không có phép.
2. **Lưu URL dẫn tới đúng trang tin.** URL trang danh sách kèm `#fragment` không phải
   trang tin — nó fetch được nhưng trả về nội dung khác, và làm mọi bước sau thất bại.
   Bài học này đã trả giá trong lần chạy thủ công: những tin chỉ lưu được link trang
   danh sách thì hai tháng sau không còn cách nào kiểm tra lại xem còn mở hay không.

### 3.2 Giao diện connector

Mọi nguồn cài cùng một protocol, để thêm nguồn mới không phải sửa worker:

```python
class JobSource(Protocol):
    id: str
    tier: SourceTier

    async def search(self, query: SearchQuery) -> list[RawJob]: ...
    async def detail(self, ref: str) -> RawJobDetail | None: ...
    async def health(self) -> SourceHealth: ...
```

`health()` port trực tiếp logic Step 4.75 của `job-scraper/SKILL.md`: parser hỏng thường
exit 0 với kết quả rỗng hoặc trường null, nên phải phát hiện bằng chất lượng dữ liệu
chứ không bằng exit code. Rate limit **không** tính là hỏng.

### 3.3 Gọi scraper TypeScript từ Python

Các CLI trong `.agents/skills/*/cli/` đã có test, đã xử lý retry và timeout. Viết lại
bằng Python là làm hỏng thứ đang chạy tốt. Bọc chúng lại:

```python
class BunCliSource:
    """Chạy một portal CLI có sẵn và parse JSON nó in ra."""

    def __init__(self, source_id: str, cli_path: Path, tier: SourceTier):
        ...

    async def search(self, query: SearchQuery) -> list[RawJob]:
        proc = await asyncio.create_subprocess_exec(
            "bun", "run", str(self.cli_path), "search",
            *self._flags(query), "--format", "json",
            stdout=PIPE, stderr=PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            raise SourceError(self.source_id, err.decode()[:500])
        return [RawJob.model_validate(r) for r in json.loads(out)["results"]]
```

Lưu ý vận hành: container phải có `bun`, và timeout là bắt buộc — `timeout` của GNU
không có trên macOS nên đừng dựa vào nó, dùng `asyncio.wait_for`.

### 3.4 Chuẩn hoá khi ingest

Những thứ sau suy ra bằng code, **không bằng LLM**:

| Trường | Cách suy ra |
|---|---|
| `contract_type` | Regex trên tiêu đề: `praktik` → internship, `werkstud\|working student` → working_student, có cả hai → both |
| `hours_per_week` | Regex trên mô tả: `(\d+)\s*(Stunden\|Std\.?)\s*(pro\s*Woche\|/\s*Woche)` |
| `lat`/`lon` | Geocode tên địa điểm (Nominatim, cache theo tên) |
| `deadline` | Pattern ngày trong mô tả, `NULL` nếu không có — **không đoán** |
| `is_active` | `detail()` trả về cờ đóng tin, hoặc URL 404 |

Quy tắc "không đoán" nằm sẵn trong `job-scraper/SKILL.md` và nên giữ: `NULL` nghĩa là
tin không nêu, thiếu khoá nghĩa là bản ghi có trước khi có trường đó. Không bao giờ
backfill bằng phỏng đoán.

---

## 4. Lớp agent

Năm agent, mỗi agent là một hàm thuần: nhận dữ liệu có cấu trúc, trả về dữ liệu có
cấu trúc. Không agent nào tự ý gọi agent khác — worker điều phối.

### 4.0 Lớp provider: không khoá vào một nhà cung cấp

Yêu cầu: đổi model và đổi cả nhà cung cấp mà không sửa code agent. Cách làm là **đưa
lựa chọn model ra khỏi code, thành dữ liệu**, và che khác biệt giữa các nhà cung cấp
sau một protocol.

```python
class Capability(StrEnum):
    STRUCTURED_OUTPUT = "structured_output"   # schema cứng, không cần parse tay
    PROMPT_CACHE      = "prompt_cache"        # cache theo tiền tố
    BATCH             = "batch"               # xử lý theo lô, rẻ hơn
    PDF_INPUT         = "pdf_input"           # nhận PDF trực tiếp
    WEB_SEARCH        = "web_search"          # server tool tìm web
    THINKING          = "thinking"            # suy luận mở rộng

class LLMProvider(Protocol):
    name: str
    def supports(self, cap: Capability) -> bool: ...
    async def complete(self, req: CompletionRequest) -> CompletionResult: ...
```

`CompletionRequest` cố tình không mang khái niệm riêng của nhà cung cấp nào:

```python
@dataclass
class CompletionRequest:
    system: list[TextPart]         # part nào cache được thì gắn cờ, provider tự quyết
    messages: list[Message]
    output_schema: type[BaseModel] | None
    max_tokens: int
    effort: Effort                 # low | medium | high  (ánh xạ theo provider)
    attachments: list[Attachment]  # PDF, ảnh
    tools: list[ToolSpec]
```

**Suy giảm chức năng, không vỡ.** Khi provider thiếu một capability, adapter tự bù:

| Thiếu | Cách bù | Mất gì |
|---|---|---|
| `STRUCTURED_OUTPUT` | Nhúng JSON Schema vào prompt, validate bằng Pydantic, thử lại tối đa 2 lần nếu sai | Thêm token, thỉnh thoảng thêm một lượt gọi |
| `PROMPT_CACHE` | Gửi nguyên prompt | **Đắt hơn nhiều.** Đây là capability đáng tiền nhất |
| `BATCH` | Chạy tuần tự có giới hạn song song | Chậm hơn, mất mức giảm giá |
| `PDF_INPUT` | Trích text bằng `pypdf` ở phía server rồi gửi text | Mất bố cục, giảm độ chính xác của Agent 1 |
| `WEB_SEARCH` | Dùng search API riêng (Brave/Tavily) rồi fetch, đưa vào prompt | Thêm nhà cung cấp, thêm code |

Nguyên tắc: **agent gọi `provider.supports()` để chọn đường đi, không bao giờ kiểm tra
tên nhà cung cấp.** Viết `if provider.name == "anthropic"` là chỗ khoá cứng quay lại.

**Cấu hình model là dữ liệu:**

```sql
CREATE TABLE agent_configs (
    agent           TEXT PRIMARY KEY,   -- cv_reader | job_finder | evaluator | writer | reviewer | interview_coach
    provider        TEXT NOT NULL,      -- anthropic | openai | google | local ...
    model           TEXT NOT NULL,
    effort          TEXT NOT NULL DEFAULT 'medium',
    max_tokens      INTEGER NOT NULL,
    use_batch       BOOLEAN NOT NULL DEFAULT false,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Đổi model là một câu `UPDATE`, không phải một lần deploy. Đây chính là cơ chế để
"cải thiện dần trong quá trình phát triển" như bạn muốn: đo bằng bảng `llm_calls`,
đổi bằng `agent_configs`, so lại trên eval set.

**Cấu hình khởi điểm** (ngày 16/10/2026, dùng Anthropic):

```sql
INSERT INTO agent_configs (agent, provider, model, effort, max_tokens, use_batch) VALUES
  ('cv_reader',       'anthropic', 'claude-haiku-5-5',  'low',    8000,  false),
  ('job_finder',      'anthropic', 'claude-haiku-5-5',  'low',    2000,  false),
  ('evaluator',       'anthropic', 'claude-sonnet-5-5', 'medium', 4000,  true),
  ('writer',          'anthropic', 'claude-opus-5-5',   'high',   16000, false),
  ('reviewer',        'anthropic', 'claude-sonnet-5-5', 'high',   8000,  false),
  ('interview_coach', 'anthropic', 'claude-opus-5-5',   'high',   16000, false);
```

Lưu ý khi viết adapter Anthropic: `effort` map vào `output_config={"effort": ...}`,
và **không dùng `budget_tokens`** — tham số đó đã bị loại bỏ trên các model hiện tại
và sẽ trả về lỗi 400. Thinking bật bằng `thinking={"type": "adaptive"}`.

### 4.1 Agent 1 — CV Reader

**Việc:** PDF/DOCX → JSON hồ sơ. Chạy một lần mỗi file, kết quả cache theo `content_hash`.

**Model:** `claude-haiku-5-5`, `output_config={"effort": "low"}`.
Trích xuất theo schema cố định, không cần phán đoán.

**Đầu ra:**

```python
class Profile(BaseModel):
    name: str
    location: str | None
    email: str | None
    phone: str | None
    languages: list[Language]          # code + level, nguyên văn như CV ghi
    education: list[Education]
    experience: list[Experience]       # title, company, period, bullets
    skills: SkillSet                   # primary / secondary / tools
    certifications: list[Certification]
    confidence_notes: list[str]        # chỗ model không chắc, để user sửa
```

Gọi PDF trực tiếp bằng content block `document`, không cần thư viện parse riêng:

```python
resp = client.messages.parse(
    model="claude-haiku-5-5",
    max_tokens=8000,
    output_config={"format": Profile, "effort": "low"},
    messages=[{"role": "user", "content": [
        {"type": "document", "source": {"type": "file", "file_id": file_id}},
        {"type": "text", "text": EXTRACT_INSTRUCTIONS},
    ]}],
)
```

**Bắt buộc:** cho user sửa kết quả trước khi dùng (`profiles.user_edited`). Hồ sơ sai
làm sai toàn bộ các bước sau, và `confidence_notes` chỉ ra chỗ cần xem.

### 4.2 Agent 2 — Job Finder

**Việc:** giữ `jobs` luôn mới. Đây **không phải agent LLM** mà là worker định kỳ.

```
cron (6h/lần)
  └─ với mỗi enabled source:
       ├─ search(query dựng từ search_prefs)
       ├─ chuẩn hoá + geocode + dedup_key
       ├─ UPSERT vào jobs
       └─ ghi sources.last_status
```

LLM chỉ xuất hiện ở một chỗ duy nhất: **sinh biến thể từ khoá**. Người dùng gõ
"thực tập lập trình", hệ thống cần truy vấn bằng `Praktikum Softwareentwicklung`,
`Werkstudent Entwicklung`, `Software Engineering Intern`. Việc đó dùng
`claude-haiku-5-5`, chạy khi user đổi tiêu chí, kết quả cache vào `search_prefs`.

Phần lọc nằm hoàn toàn trong SQL:

```sql
SELECT j.* FROM jobs j
WHERE j.is_active
  AND j.posted_date > now() - interval '21 days'
  AND (j.deadline IS NULL OR j.deadline >= current_date)
  AND j.contract_type = ANY(:contract_types)
  AND (j.hours_per_week IS NULL OR j.hours_per_week >= :min_hours)
  AND earth_distance(ll_to_earth(j.lat, j.lon),
                     ll_to_earth(:home_lat, :home_lon)) < :max_commute_m
  AND NOT EXISTS (SELECT 1 FROM evaluations e
                  WHERE e.job_id = j.id AND e.user_id = :uid
                    AND e.profile_id = :pid);
```

### 4.3 Agent 3 — Evaluator

**Việc:** chấm một tin so với một hồ sơ. Đây là agent chạy nhiều nhất.

**Model:** `claude-sonnet-5-5`. Cần phán đoán thật nhưng khối lượng lớn.

**Hai cơ chế giảm giá, dùng đồng thời:**

1. **Prompt caching.** Khung đánh giá (~3.000 token) và hồ sơ (~1.500 token) giống nhau
   ở mọi lần gọi của cùng một user. Đặt trước breakpoint, token cached rẻ hơn 10 lần.

   ```python
   system=[
       {"type": "text", "text": EVAL_FRAMEWORK},                      # ổn định toàn hệ thống
       {"type": "text", "text": profile_json,
        "cache_control": {"type": "ephemeral", "ttl": "1h"}},         # ổn định theo user
   ]
   messages=[{"role": "user", "content": posting_text}]               # phần thay đổi, đặt cuối
   ```

   Cache khớp theo **tiền tố**, nên tuyệt đối không nhét timestamp hay UUID vào system
   prompt. Kiểm chứng bằng `usage.cache_read_input_tokens` — nếu nó bằng 0 qua nhiều
   lần gọi thì có thứ gì đang phá cache.

2. **Batch API.** Scraper chạy đêm, chấm điểm xong trước khi user mở app. Giảm 50%.

   ```python
   batch = client.messages.batches.create(requests=[
       Request(custom_id=f"{user_id}:{job_id}", params=MessageCreateParamsNonStreaming(...))
       for job_id in pending
   ])
   ```

   Kết quả trả về **không theo thứ tự** — luôn khớp bằng `custom_id`.

**Đầu ra:** cấu trúc này port trực tiếp từ `04-job-evaluation.md`, đừng thiết kế lại:

```python
class Evaluation(BaseModel):
    gates: Gates                 # eligibility / language / location / contract: PASS|FLAG|FAIL
    technical_skills: int        # 0-100
    experience_match: int
    behavioral_fit: int
    career_alignment: int
    overall: int                 # trung bình có trọng số: 30/25/15/30
    verdict: Verdict             # strong|good|moderate|weak|poor
    strengths: list[str]         # 1-3 gạch đầu dòng
    gaps: list[str]
    notes: str | None            # giải thích gate không PASS, có trích nguyên văn tin
```

**Gate chạy trước khi tính điểm và có quyền veto.** Một tin 90 điểm mà fail gate khoảng
cách thì bị loại, không phải xếp đầu. Quy tắc này đã có trong `rank.md` dòng 125.

### 4.4 Agent 4 — Document Writer

**Việc:** sinh CV và Anschreiben nhắm vào một tin cụ thể. Chỉ chạy khi user bấm nút.

**Model:** `claude-opus-5-5` soạn thảo, `claude-sonnet-5-5` phê bình. Đây là đầu ra
user thực sự gửi cho nhà tuyển dụng, chất lượng quan trọng hơn giá.

**Luồng ba bước, port từ `apply.md`:**

```
[1] DRAFTER   opus-5-5    hồ sơ + tin + template → cv.tex, letter.tex
      ↓
[2] REVIEWER  sonnet-5-5  nhận bản nháp inline, trả về JSON array các edit
      ↓                   [{file, old_string, new_string, rationale}, ...]
[3] DRAFTER   opus-5-5    áp dụng edit, compile, kiểm tra
```

Thiết kế reviewer trả về **JSON array edit cụ thể** thay vì văn xuôi là điểm hay nhất
của repo gốc — nó biến phản hồi thành thứ áp dụng được bằng máy, không cần diễn giải.

**Rào chắn chống bịa (bắt buộc giữ):** mọi câu trong giấy tờ phải truy được về
`profiles.data`. Repo gốc gọi đây là Factual Grounding Audit, và nó nghiêm ngặt có lý —
một CV bịa kỹ năng là thứ làm người dùng mất việc chứ không phải được việc.

**Biên dịch:** chạy trong worker, không trong request.

| Loại | Lệnh | Vì sao không đổi được |
|---|---|---|
| CV | `lualatex` | `pdflatex` lỗi font-expansion với `fontawesome5` trên MiKTeX mới |
| Anschreiben | `xelatex` | `cover.cls` cần `fontspec` cho font Lato/Raleway |

Sau khi compile, chạy kiểm tra tự động (port `tools/verify_pdf.py`): số trang, lớp text
trích xuất được, email và số điện thoại có xuất hiện dạng text thật cho ATS đọc.

### 4.5 Agent 5 — Interview Coach

**Việc:** khi tracker chuyển một ứng tuyển sang `interview`, dựng bộ luyện phỏng vấn
riêng cho vị trí đó.

**Kích hoạt:** trigger trên `application_events`, không phải người dùng bấm nút.

```sql
CREATE OR REPLACE FUNCTION queue_interview_prep() RETURNS trigger AS $$
BEGIN
    IF NEW.to_status = 'interview' THEN
        INSERT INTO task_queue (kind, payload)
        VALUES ('interview_prep', jsonb_build_object('application_id', NEW.application_id));
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
```

**Model:** `claude-opus-5-5`, effort `high`, kèm server tool web search. Đây là agent
chạy ít nhất nhưng giá trị cao nhất — một lần phỏng vấn trượt đắt hơn nhiều lần mọi
khoản token bạn tiết kiệm được ở ba agent kia.

**Bốn bước, port từ `.claude/commands/interview.md`:**

```
[1] Nạp ngữ cảnh    hồ sơ, tin tuyển dụng nguyên văn, giấy tờ đã gửi,
                    evaluation (strengths/gaps), ghi chú trong tracker
      ↓
[2] Nghiên cứu      web search: công ty, sản phẩm, tin tức gần đây, ngành,
                    và góc nhìn của từng người phỏng vấn nếu biết tên
      ↓
[3] Dựng bộ prep    câu hỏi khả năng cao + câu hỏi tình huống + ánh xạ STAR
                    + câu hỏi khó + câu hỏi nên hỏi lại họ
      ↓
[4] Chế độ luyện    hỏi từng câu, người dùng trả lời, agent nhận xét
```

**Đầu ra:**

```python
class InterviewPack(BaseModel):
    company_brief: CompanyBrief        # mỗi khẳng định kèm source_url
    likely_questions: list[Question]   # behavioral | technical | situational
    star_mapping: list[StarAnswer]     # câu hỏi → kinh nghiệm thật của user
    tough_questions: list[Question]    # gồm cả gap từ evaluation
    questions_to_ask: list[str]
    red_flags_to_probe: list[str]      # dùng để tránh thất vọng sau khi nhận việc
```

```sql
CREATE TABLE interview_packs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id  UUID NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    data            JSONB NOT NULL,
    model_used      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Chế độ luyện: lưu lại để biết câu nào đã tập và tập tới đâu
CREATE TABLE practice_answers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pack_id         UUID NOT NULL REFERENCES interview_packs(id) ON DELETE CASCADE,
    question_idx    INTEGER NOT NULL,
    answer_text     TEXT NOT NULL,
    feedback        TEXT,
    confidence      SMALLINT,          -- 1-5, người dùng tự đánh giá
    answered_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Ba quy tắc bắt buộc, lấy từ repo gốc:**

1. **Mọi khẳng định về công ty phải có `source_url`.** `interview.md` Step 2 yêu cầu
   xác minh độc lập, và tuyệt đối không tin URL nằm trong nội dung tin tuyển dụng —
   đó là dữ liệu chưa kiểm chứng.
2. **Câu trả lời STAR chỉ dựng từ kinh nghiệm thật trong hồ sơ.** `07-interview-prep.md`
   ghi rõ: chỉ có 2 ví dụ STAR thật, không bịa thêm. Agent phải chịu ràng buộc như
   Agent 4 — mục 11.6 phần A áp dụng cho cả hai.
3. **Nghiên cứu người phỏng vấn chỉ dựa trên thông tin công khai**, và không suy diễn
   quá những gì đọc được. Một hiring manager thường hỏi về động lực và hoà nhập; một
   engineer hỏi sâu kỹ thuật; HR soi dòng thời gian trong CV.

### 4.6 Bảng tổng hợp model và chi phí

| Agent | Model | Effort | Tần suất | Chi phí ước lượng |
|---|---|---|---|---|
| 1. CV Reader | `claude-haiku-5-5` | low | 1 lần/CV | ~$0,002/CV |
| 2. Job Finder | `claude-haiku-5-5` (chỉ sinh từ khoá) | low | khi đổi tiêu chí | ~$0,001/lần |
| 3. Evaluator | `claude-sonnet-5-5` | medium | 50 tin/ngày/user | ~$0,65/ngày, còn ~$0,33 nếu dùng Batch |
| 4. Writer | `claude-opus-5-5` + `claude-sonnet-5-5` | high | khi user ứng tuyển | ~$0,10/bộ hồ sơ |
| 5. Interview Coach | `claude-opus-5-5` + web search | high | khi có lịch phỏng vấn | ~$0,30/bộ prep |

Con số là bậc độ lớn để lập kế hoạch, không phải báo giá. Giá hiện hành:
Haiku 5.5 $0,10/$0,50 · Sonnet 5.5 $2/$10 · Opus 5.5 $4/$20 (mỗi 1M token in/out),
cache read $0,20/1M. Bảng `llm_calls` tồn tại để thay ước lượng bằng số đo thật.

Mặc định của Anthropic là Opus 5.5 cho mọi việc. Việc hạ Agent 1–3 xuống Haiku/Sonnet
là đánh đổi có ý thức để tối ưu chi phí; nếu chất lượng chấm điểm không đạt, nâng
Agent 3 lên Opus 5.5 trước khi sửa prompt.

---

## 5. API

```
POST   /auth/login                      → JWT
POST   /auth/invite                     → chỉ owner

GET    /me
POST   /me/documents                    → upload CV/bằng cấp (multipart)
POST   /me/documents/{id}/parse         → kích hoạt Agent 1, trả job_id
GET    /me/profile                      → hồ sơ hiện tại
PATCH  /me/profile                      → user sửa, set user_edited
PUT    /me/search-prefs

GET    /sources                         → đã lọc theo tier, mục 3.1
PATCH  /sources/{id}                    → bật/tắt
POST   /sources/{id}/run                → ingest ngay, trả job_id

GET    /jobs?verdict=&contract=&page=   → đã lọc + kèm evaluation nếu có
GET    /jobs/{id}
POST   /jobs/{id}/evaluate              → kích hoạt Agent 3, trả job_id

POST   /applications                    → tạo từ job_id, sinh folder_slug
GET    /applications
PATCH  /applications/{id}               → đổi status
POST   /applications/{id}/generate      → kích hoạt Agent 4, trả job_id
GET    /applications/{id}/artifacts
GET    /artifacts/{id}/download

GET    /tasks/{job_id}/events           → SSE: tiến độ tác vụ nền
DELETE /me                              → xoá toàn bộ dữ liệu, mục 9.4
```

Mọi endpoint kích hoạt agent đều trả `{"task_id": ...}` ngay lập tức. Frontend theo dõi
qua SSE. Đây là hệ quả trực tiếp của nguyên tắc ở mục 1.

---

## 6. Frontend

```
src/
├── routes/
│   ├── onboarding/      upload CV → xem và sửa hồ sơ → đặt tiêu chí
│   ├── jobs/            danh sách + bộ lọc; card hiển thị verdict và gate
│   ├── jobs/$id/        chi tiết tin + bảng điểm 5 chiều + strengths/gaps
│   ├── applications/    kanban theo status
│   ├── applications/$id/ giấy tờ, tải PDF, ghi chú
│   └── settings/        nguồn dữ liệu, tình trạng, chi phí
├── lib/
│   ├── api.ts           client sinh từ OpenAPI của FastAPI
│   └── tasks.ts         hook SSE theo dõi tác vụ nền
└── components/
    ├── GateBadge.tsx    PASS/FLAG/FAIL — FLAG phải nhìn thấy rõ, không ẩn
    └── ScoreTable.tsx
```

Một yêu cầu UX không thương lượng: **gate FLAG phải hiện ngay trên card**, không chôn
trong trang chi tiết. Ví dụ thật: một tin Werkstudent cần hỏi nhà tuyển dụng có ký
giấy xác nhận giờ không — người dùng phải thấy điều đó trước khi bỏ công viết hồ sơ.

---

## 7. Lưu trữ giấy tờ theo công ty và vị trí

### 7.1 Quy ước đặt tên — port nguyên văn, đừng thiết kế lại

`tools/job_key.py` và `documents/README.md` đã giải quyết bài toán này sau khi gặp lỗi
thật. Quy tắc:

- `<company>_<role>`, chữ thường, khoảng trắng thành `_`
- Mọi ký tự không phải chữ/số/`_` bị **loại bỏ** (nên `Novo Nordisk A/S` → `novo_nordisk_as`)
- Chuỗi `_` liên tiếp co lại thành một, cắt `_` ở hai đầu
- Tiêu đề dài bị cắt theo độ dài **kèm hash của slug đầy đủ**, để cùng một tin luôn
  cho cùng một khoá và hai tiêu đề dài khác nhau không bao giờ trùng
- Tiêu đề slugify ra rỗng (chữ không phải Latin) thì lấy id số từ URL

Hai lỗi thật mà quy tắc này chặn, ghi trong docstring của file gốc:
`deloitte_junior-cybersecurity-analyst-(ot/iot)` — dấu `/` cắt đường dẫn thành hai thư
mục; và cùng một tin Deloitte lưu hai lần vì hai lần chạy cắt tiêu đề ở chỗ khác nhau.

### 7.2 Bố cục

```
<user_id>/applications/<company>_<role>/
├── posting.md        toàn văn tin, lưu nguyên bản làm bằng chứng
├── evaluation.json   bảng điểm tại thời điểm ứng tuyển
├── cv.tex  cv.pdf
└── anschreiben.tex  anschreiben.pdf
```

`posting.md` lưu **nguyên văn, không tóm tắt** — khi phỏng vấn sau hai tháng, tin gốc
thường đã bị xoá khỏi mạng.

---

## 8. Port gì từ repo hiện tại

### 8.1 Skill → agent: cái nào giữ, cái nào bỏ

Repo hiện có 10 file skill và 12 command. Không phải tất cả đều có chỗ trong sản phẩm.

**Giữ, thành system prompt (ưu tiên cao nhất — đây là tài sản thật):**

| File | Dòng | Đi vào | Cách dùng |
|---|---|---|---|
| `04-job-evaluation.md` | 278 | **Agent 3** | Gần như nguyên văn. Gate + 5 chiều điểm + trọng số + ngưỡng |
| `03-writing-style.md` | 110 | **Agent 4** | Nguyên văn. Quy tắc "demonstrate, don't state" |
| `05-cv-templates.md` | 381 | **Agent 4** | Phần hướng dẫn nội dung → prompt; phần LaTeX → template engine |
| `06-cover-letter-templates.md` | 180 | **Agent 4** | Như trên |
| `07-interview-prep.md` | 107 | **Agent 5** | STAR format, câu hỏi khó, câu hỏi nên hỏi lại |
| `09-web-research.md` | 114 | **Agent 5** | Quy tắc xác minh nguồn, xử lý 403 |
| `01-candidate-profile.md` | 76 | **Agent 1** | Làm schema đích cho việc trích xuất |
| `02-behavioral-profile.md` | 40 | Agent 3, 4 | Chiều "behavioral fit"; đánh dấu rõ cái gì là suy đoán |

**Giữ, thành logic điều phối:**

| File | Đi vào | Phần cần port |
|---|---|---|
| `apply.md` (403 dòng) | Agent 4 | Luồng drafter → reviewer (JSON edit) → revise. Bỏ phần nói chuyện với người dùng |
| `interview.md` (111 dòng) | Agent 5 | 4 bước ở mục 4.5, kèm chế độ roleplay |
| `rank.md` (188 dòng) | Worker Agent 3 | Quy tắc deal-breaker veto, xếp hạng theo lô |
| `job-scraper/SKILL.md` (294 dòng) | Agent 2 | Logic dedup, health check Step 4.75, quy tắc không-đoán |
| `search-queries.md` (106 dòng) | Agent 2 | Mẫu truy vấn `site:` làm đường lùi WebSearch |
| `outcome.md` (243 dòng) | Mục 12 | Máy trạng thái và quy tắc khớp bản ghi |
| `08-application-forms.md` (87 dòng) | Agent 4 | Khi nhà tuyển dụng dùng form thay vì đính kèm file |

**Bỏ, không port:**

| File | Vì sao |
|---|---|
| `setup.md` (441 dòng) | Thay bằng luồng onboarding của web UI. Đây là cuộc phỏng vấn trong terminal |
| `reset.md` (284 dòng) | Thay bằng `DELETE /me` |
| `html-report.md` (146 dòng) | Frontend React thay thế hoàn toàn |
| `notion-sync.md`, `gmail-sync.md` | Tích hợp ngoài. Để Giai đoạn 6+, không phải lõi |
| `add-portal.md`, `add-template.md` | Đây là công cụ sinh code cho Claude Code. Sản phẩm có giao diện thêm nguồn riêng |
| `expand.md` (230 dòng) | Chức năng gộp vào Agent 1 (đọc nhiều tài liệu) |
| `upskill/SKILL.md` (256 dòng) | Tính năng hay nhưng ngoài luồng chính. Giai đoạn 6+ |

### 8.2 Code và tool port trực tiếp

| Tài sản | Cách dùng | Công sức |
|---|---|---|
| `.agents/skills/linkedin-search/cli/` | → bọc bằng `BunCliSource`, **không viết lại** | Thấp |
| `tools/job_key.py` | → port sang hàm Python trong app | Thấp |
| `tools/verify_pdf.py`, `verify_layout.py` | → bước kiểm tra sau compile | Thấp |
| `tools/robots_check.py` | → bắt buộc, gọi trước mọi fetch của connector `personal` | Thấp |
| `cv/*.tex`, `cover_letters/cover.cls` + font | → template engine | Trung bình |
| `seen_jobs.json` schema | → tham khảo bảng `jobs` | Đã làm, mục 2 |

Tổng kết: khoảng **1.400 dòng prompt** đi thẳng vào sản phẩm, và khoảng **1.500 dòng
command** bị bỏ vì chúng tồn tại để nói chuyện với người dùng qua terminal — việc đó
giờ là của React.

---

## 9. Lộ trình

Nguyên tắc xuyên suốt: **mỗi giai đoạn test được bằng FastAPI docs (`/docs`) trước
khi có bất kỳ dòng React nào.** Frontend chỉ dựng khi backend đã chạy đúng.

### Giai đoạn 1 — Nền móng, test qua `/docs`
Supabase project + migration. FastAPI khởi tạo. **Lớp provider (mục 4.0)** và bảng
`agent_configs`. Bọc CLI LinkedIn thành connector đầu tiên. Agent 1 và Agent 3 chạy thật.
Chưa có auth (một user duy nhất), chưa có frontend.
**Tiêu chí:** qua Swagger UI, upload được CV → xem hồ sơ JSON → chạy ingest LinkedIn →
xem danh sách tin đã chấm điểm. Toàn bộ bằng `curl` hoặc `/docs`.

### Giai đoạn 2 — Giấy tờ và tracker, vẫn test qua API
Agent 4 (drafter–reviewer), compile LaTeX trong worker, kiểm tra PDF. Kèm
`job_user_state`, `application_events`, `application_tasks` (mục 12 và 13).
**Tiêu chí:** `POST /applications/{id}/generate` trả về PDF compile sạch; đổi status
sinh đúng một bản ghi sự kiện; task roadmap sinh đúng theo điều kiện `when`.

### Giai đoạn 3 — Frontend
React + Vite. Onboarding, danh sách việc có nút đánh dấu, trang chi tiết đánh giá,
kanban, trang roadmap. Client sinh từ OpenAPI của FastAPI.
**Tiêu chí:** làm trọn một vòng từ upload CV tới sinh hồ sơ mà không mở terminal.

### Giai đoạn 4 — Agent 5 và luyện phỏng vấn
Interview Coach, web search có xác minh nguồn, chế độ practice.
**Tiêu chí:** đánh dấu `interview` trên kanban → bộ prep tự xuất hiện, mọi khẳng định
về công ty có link nguồn kiểm tra được.

### Giai đoạn 5 — Thêm nguồn
StepStone, Indeed, các ATS công khai, Arbeitnow. Health check. Đường lùi WebSearch.
**Tiêu chí:** một nguồn hỏng không làm sập ingest, và trạng thái hỏng hiện trên UI.

### Giai đoạn 6 — Nhiều người dùng
Supabase Auth, RLS (mục 2.2), luồng mời, thi hành cờ `is_owner` cho tier `personal`.
**Tiêu chí:** người quen đăng nhập, không thấy dữ liệu của bạn, **không chạy được
connector tier personal**.

### Giai đoạn 7 — Lớp retrieval
pgvector, Voyage AI, truy xuất hỗn hợp, nối đất Agent 4 và Agent 5 bằng
`artifact_citations`.
**Điều kiện khởi động:** kho tài liệu một người dùng vượt ~15–20 nghìn token, hoặc đã
tracking đủ nhiều vị trí tương đồng để việc tái dùng cách diễn đạt cũ có giá trị.
Làm sớm hơn là dựng hạ tầng cho bài toán chưa tồn tại — mục 11.1.
**Tiêu chí:** mỗi gạch đầu dòng sinh ra đều join được tới chunk nguồn.

### 9.4 Nghĩa vụ dữ liệu ở quy mô "vài người quen"

Chưa cần bộ GDPR đầy đủ, nhưng bốn thứ sau nên có từ Giai đoạn 3 vì thêm sau rất đau:

1. **`DELETE /me` xoá thật** — cả bản ghi DB lẫn file trong object storage. Thiết kế
   `ON DELETE CASCADE` ở mục 2 là để phục vụ việc này.
2. **Một dòng nói rõ dữ liệu đi đâu** khi người dùng upload CV: nội dung CV được gửi
   tới API của Anthropic để xử lý.
3. **Mã hoá at-rest** cho object storage. CV chứa địa chỉ, số điện thoại, ngày sinh.
4. **Đừng log nội dung CV.** Log `document_id` và số token, không log nội dung.

---

## 10. Rủi ro còn mở

| Rủi ro | Mức độ | Hướng xử lý |
|---|---|---|
| Nguồn public phủ ít tin hơn LinkedIn | **Cao** | Đo thật ở Giai đoạn 1: Arbeitnow + ATS phủ được bao nhiêu phần trăm so với con số bạn thấy khi chạy thủ công. Nếu quá thấp thì giá trị sản phẩm cho người khác bị giới hạn, và đó là thông tin cần biết sớm |
| Điều kiện thương mại của Adzuna | Trung bình | Đọc điều khoản trước khi tích hợp. Có nguồn nói dùng thương mại cần giấy phép sau 14 ngày thử |
| ATS feed đổi cấu trúc | Trung bình | `health()` phát hiện theo chất lượng dữ liệu, không theo exit code |
| Chi phí vượt dự kiến | Thấp | Bảng `llm_calls` có từ đầu; đặt ngưỡng cảnh báo theo user |
| Chất lượng chấm điểm của Sonnet không đạt | Trung bình | Dựng eval set khoảng 30 tin đã chấm tay, so Sonnet với Opus trước khi chốt |
| Geocode rate limit (Nominatim) | Thấp | Cache theo tên địa điểm; tên lặp lại rất nhiều |

---

## 11. Lớp retrieval (RAG)

### 11.1 RAG đáng giá ở đâu, và không đáng ở đâu

Trước khi dựng, cần tách rõ bốn việc, vì chỉ hai trong số đó cần retrieval thật:

| Việc | Cần RAG? | Lý do |
|---|---|---|
| So một CV với một tin tuyển dụng (Agent 3) | **Không** | Hồ sơ ~1.500 token, tin ~2.000 token. Cả hai nhét trọn vào prompt và được cache. Thêm retrieval ở đây chỉ làm chậm và làm mất ngữ cảnh |
| Lọc theo khu vực, loại hợp đồng, số giờ | **Không** | Đây là `WHERE` trong SQL, mục 4.2 |
| Trả lời "kinh nghiệm nào của tôi khớp yêu cầu này?" khi kho tài liệu lớn dần | **Có** | Khi người dùng đã tải lên nhiều CV, bằng cấp, thư giới thiệu, mô tả dự án, chứng chỉ — tổng cộng vượt xa mức nhét vừa prompt |
| Tìm việc theo ý nghĩa: "tôi muốn làm data pipeline" | **Có** | Từ khoá không bắt được tin ghi "ETL-Prozesse aufbauen". Đây là chỗ embedding thắng rõ |

Nói thẳng: **nếu bạn chỉ có một CV, RAG là thừa.** Nó bắt đầu trả công khi kho tài liệu
của một người dùng vượt khoảng 15–20 nghìn token, hoặc khi bạn muốn tìm việc theo ngữ nghĩa.
Vì vậy nó nằm ở Giai đoạn 5 trong lộ trình, không phải Giai đoạn 1.

Nhưng có một lý do thứ hai, mạnh hơn, để làm nó:

> **RAG biến quy tắc chống bịa từ một câu trong prompt thành một cấu trúc dữ liệu.**
> Hiện tại `apply.md` yêu cầu mọi câu trong giấy tờ phải truy được về hồ sơ, và việc kiểm
> tra là model tự soát lại chính mình. Với retrieval, mỗi gạch đầu dòng Agent 4 viết ra
> **mang theo id của chunk nguồn**. Kiểm tra trở thành join bảng, không còn là niềm tin.

### 11.2 Hạ tầng: pgvector, không dùng vector DB riêng

Bạn đã có Postgres. Thêm `pgvector` thay vì dựng Qdrant/Milvus/Chroma riêng:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_kind     TEXT NOT NULL,      -- document | application_letter | job
    source_id       UUID NOT NULL,      -- documents.id | artifacts.id | jobs.id
    section         TEXT,               -- 'experience/SMC' | 'requirements' ...
    content         TEXT NOT NULL,
    token_count     INTEGER NOT NULL,
    embedding       vector(1024),       -- khớp số chiều của model đã chọn
    tsv             tsvector GENERATED ALWAYS AS
                        (to_tsvector('german', content)) STORED,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX chunks_vec ON chunks
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_fts ON chunks USING gin (tsv);
CREATE INDEX chunks_user ON chunks (user_id, source_kind);
```

Ba lý do chọn pgvector:

1. **Một service ít hơn phải vận hành.** Ở quy mô vài người dùng, mỗi người vài nghìn
   chunk, pgvector nhanh hơn mức cần thiết. Vector DB riêng chỉ đáng từ hàng triệu vector.
2. **Nhất quán giao dịch.** Xoá user là `ON DELETE CASCADE` xoá luôn chunk. Với vector DB
   ngoài, bạn phải tự đồng bộ việc xoá — và đó chính là chỗ nghĩa vụ xoá dữ liệu ở mục 9.4
   hay bị rò.
3. **Full-text tiếng Đức miễn phí.** `to_tsvector('german', ...)` có stemming tiếng Đức
   sẵn trong Postgres. Xem 11.4 vì sao điều này quan trọng.

### 11.3 Embedding model

**Anthropic không có endpoint embeddings.** [Tài liệu chính thức của Claude](https://docs.claude.com/en/docs/build-with-claude/embeddings)
nói họ không làm model embedding riêng và giới thiệu **Voyage AI** làm nhà cung cấp.
Dòng hiện tại là [Voyage 4](https://blogs.novita.ai/anthropic-embeddings-api/) (`voyage-4-lite`
/ `voyage-4` / `voyage-4-large`), gọi qua `POST https://api.voyageai.com/v1/embeddings`
bằng SDK riêng của Voyage, **không** theo định dạng OpenAI.

Lưu ý: giá và tên phiên bản ở trên lấy từ blog bên thứ ba, không phải trang chính thức
của Voyage. **Kiểm tra lại trên trang của họ trước khi chốt**, và quan trọng hơn là xác
nhận số chiều vector của model bạn chọn, vì `vector(1024)` ở trên phải khớp — đổi model
sau này nghĩa là phải embed lại toàn bộ kho.

Một ràng buộc kiến trúc: đây là **nhà cung cấp thứ hai** bên cạnh Anthropic. Nghĩa là
thêm một API key, thêm một điểm lỗi, và nếu có người dùng EU thì thêm một bên xử lý dữ
liệu cần nêu trong thông báo ở mục 9.4.

### 11.4 Truy xuất hỗn hợp (hybrid), không chỉ vector

Đây là điểm dễ làm sai nhất. Tin tuyển dụng kỹ thuật đầy **thuật ngữ chính xác** mà
embedding xử lý tệ: `VBA`, `Angular 18`, `Pflichtpraktikum`, `360 Stunden`, `C1`.
Tìm bằng vector thuần sẽ trả về "lập trình viên frontend" khi bạn hỏi "Angular 18",
rồi bỏ sót tin ghi đúng chữ đó.

Giải pháp: chạy cả hai rồi hợp nhất bằng Reciprocal Rank Fusion.

```python
async def retrieve(user_id: UUID, query: str, k: int = 8) -> list[Chunk]:
    vec = await voyage.embed(query, model=EMBED_MODEL, input_type="query")

    dense = await db.fetch("""
        SELECT id, 1 - (embedding <=> $2) AS score FROM chunks
        WHERE user_id = $1 ORDER BY embedding <=> $2 LIMIT 30
    """, user_id, vec)

    sparse = await db.fetch("""
        SELECT id, ts_rank(tsv, websearch_to_tsquery('german', $2)) AS score
        FROM chunks
        WHERE user_id = $1 AND tsv @@ websearch_to_tsquery('german', $2)
        ORDER BY score DESC LIMIT 30
    """, user_id, query)

    # RRF: điểm = Σ 1/(60 + rank). Hằng số 60 là giá trị thông dụng, tinh chỉnh sau khi đo.
    return fuse(dense, sparse, k=k)
```

### 11.5 Chia chunk

Đừng dùng cửa sổ token cố định. Tài liệu ở đây có cấu trúc rõ, hãy tôn trọng nó:

| Loại nguồn | Cách chia | Vì sao |
|---|---|---|
| CV | Một chunk cho mỗi vị trí công việc, mỗi bằng cấp, mỗi nhóm kỹ năng | Agent 4 cần trích "kinh nghiệm ở SMC" như một khối nguyên vẹn, không phải nửa đoạn |
| Bằng cấp, chứng chỉ | Một chunk mỗi file (chúng vốn ngắn) | Chia nhỏ không thêm gì |
| Thư giới thiệu | Theo đoạn | Mỗi đoạn thường nói một phẩm chất |
| Anschreiben đã gửi | Theo đoạn, kèm metadata `company` + `role` | Để tái dùng cách diễn đạt cũ: "mình đã mô tả việc tự động hoá ở SMC thế nào?" |
| Tin tuyển dụng | Theo khối yêu cầu | Chỉ embed khi bật tìm việc theo ngữ nghĩa, xem 11.6 |

Agent 1 đã trả về hồ sơ có cấu trúc (mục 4.1), nên ranh giới chunk lấy trực tiếp từ đó —
không cần model thứ hai chỉ để chia đoạn.

### 11.6 Hai luồng dùng retrieval

**A. Nối đất cho Agent 4 (giá trị chính)**

```
Agent 4 soạn thảo
  └─ với mỗi yêu cầu trong tin:
       retrieve(user_id, requirement_text, k=5)
         → chunk ứng viên, kèm id
       → model viết gạch đầu dòng, BẮT BUỘC trích dẫn chunk id đã dùng
       → lưu vào artifact_citations
```

```sql
CREATE TABLE artifact_citations (
    artifact_id     UUID NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    claim           TEXT NOT NULL,      -- câu/gạch đầu dòng trong giấy tờ
    chunk_id        UUID REFERENCES chunks(id),
    PRIMARY KEY (artifact_id, claim)
);
```

Một câu không có `chunk_id` là câu không có nguồn. Khi đó hai lựa chọn: xoá nó, hoặc
hỏi người dùng xác nhận rồi **ghi vào kho tài liệu** để lần sau nó có nguồn thật.
Quy tắc này lấy từ `apply.md` dòng 11 và giờ được thi hành bằng khoá ngoại.

**B. Tìm việc theo ngữ nghĩa (phụ)**

Người dùng gõ một câu mô tả mong muốn, hệ thống embed nó và tìm trong chunk của `jobs`.
Chạy **sau** bộ lọc SQL ở mục 4.2, không thay thế nó: lọc cứng trước cho ra tập hợp
hợp lệ, rồi xếp hạng theo ngữ nghĩa trong tập đó. Làm ngược lại sẽ trả về những tin
rất khớp ý nghĩa nhưng ở Stuttgart.

### 11.7 Chi phí và vận hành

| Hạng mục | Ghi chú |
|---|---|
| Embed kho tài liệu một người dùng | Vài chục nghìn token, chạy một lần. Không đáng kể |
| Embed tin tuyển dụng | Đây mới là phần tốn: mỗi tin ~2.000 token, 50 tin/ngày. Chỉ bật khi dùng luồng B |
| Embed câu truy vấn | Vài chục token mỗi lần tìm |
| Embed lại khi đổi model | **Toàn bộ kho.** Vì vậy ghi `embed_model` vào bảng `chunks` ngay từ đầu |

Thêm một cột mà nhiều người quên:

```sql
ALTER TABLE chunks ADD COLUMN embed_model TEXT NOT NULL;
```

Không có nó, ngày bạn đổi model embedding sẽ không biết chunk nào đã cũ, và việc so sánh
hai vector sinh bởi hai model khác nhau cho ra kết quả vô nghĩa mà **không báo lỗi**.

---

## 12. Theo dõi trạng thái do người dùng tự đánh dấu

Mục 2 đã có `applications.status`, nhưng nó chỉ tồn tại **sau khi** người dùng quyết định
ứng tuyển. Thiếu hai thứ: trạng thái người dùng tự gắn cho một tin *trước* khi ứng tuyển,
và lịch sử thay đổi trạng thái.

### 12.1 Trạng thái người dùng gắn cho tin

```sql
CREATE TABLE job_user_state (
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id      UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    mark        TEXT NOT NULL,      -- new | interested | maybe | hidden
    pinned      BOOLEAN NOT NULL DEFAULT false,
    note        TEXT,               -- ghi chú riêng, ví dụ "đã gọi hỏi về 360h"
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, job_id)
);
```

Tách khỏi `evaluations` có chủ ý: **điểm là ý kiến của máy, `mark` là ý kiến của người.**
Người dùng ẩn một tin dù máy cho 85 điểm là chuyện hợp lệ và hệ thống phải tôn trọng.
Ngược lại, gộp hai thứ vào một bảng sẽ khiến mỗi lần chấm lại (khi user cập nhật CV)
ghi đè mất lựa chọn thủ công của họ.

Quy tắc hiển thị: tin `hidden` không hiện ở danh sách chính nhưng **không bị xoá** và
vẫn đếm trong thống kê. Dedup vẫn dựa vào `jobs.dedup_key`, nên tin đã ẩn không quay lại
ở lần quét sau.

### 12.2 Lịch sử trạng thái ứng tuyển

```sql
CREATE TABLE application_events (
    id              BIGSERIAL PRIMARY KEY,
    application_id  UUID NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    from_status     TEXT,
    to_status       TEXT NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,   -- user sửa được: ngày gửi thật
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    origin          TEXT NOT NULL,          -- manual | email_sync | system
    note            TEXT
);

CREATE INDEX app_events_app ON application_events (application_id, occurred_at);
```

Bảng này **chỉ ghi thêm, không sửa, không xoá**. `applications.status` là trạng thái hiện
tại cho truy vấn nhanh; `application_events` là sự thật lịch sử.

Tách `occurred_at` khỏi `recorded_at` là chi tiết nhỏ nhưng quan trọng: người dùng
thường ghi lại việc đã xảy ra ba ngày trước. Thống kê "bao lâu mới có phản hồi" phải
tính theo `occurred_at`, nếu không mọi số đo đều lệch.

### 12.3 Máy trạng thái

```
       ┌──────────────── withdrawn ◄─── (từ bất kỳ trạng thái nào)
       │
draft ──► submitted ──► screening ──► interview ──► offer ──► accepted
                │            │            │          │
                └────────────┴────────────┴──────────┴──► rejected
                │
                └──► ghosted        (tự động: submitted + không đổi sau N ngày)
```

Hai trạng thái đáng giải thích:

- **`ghosted`** — hệ thống tự đặt khi `submitted` quá 30 ngày không có sự kiện mới
  (`origin = 'system'`). Đây là kết cục phổ biến nhất của ứng tuyển thực tập và nếu
  không có nhãn riêng thì nó lẫn vào `submitted`, làm thống kê sai. Người dùng ghi đè được.
- **`withdrawn`** — vào được từ mọi trạng thái, vì người dùng có thể rút bất cứ lúc nào.

Chuyển trạng thái **không hạn chế nghiêm ngặt**: người dùng tự biết chuyện gì xảy ra,
và việc chặn một bước nhảy "lạ" chỉ làm họ phải nói dối dữ liệu. Hệ thống ghi nhận
mọi chuyển đổi, chỉ cảnh báo nhẹ nếu trông bất thường.

### 12.4 Việc cần làm tiếp và nhắc hẹn

```sql
ALTER TABLE applications
    ADD COLUMN next_action    TEXT,          -- 'hỏi lại HR', 'chuẩn bị phỏng vấn'
    ADD COLUMN next_action_at DATE;
```

Ba nguồn sinh nhắc hẹn tự động:

| Điều kiện | Nhắc |
|---|---|
| `jobs.deadline` còn 3 ngày, chưa `submitted` | "Hạn nộp còn 3 ngày" |
| `submitted` quá 14 ngày, chưa có sự kiện mới | "Nên hỏi lại tình hình" |
| `interview` có `next_action_at` | "Phỏng vấn ngày X — xem lại ghi chú" |

### 12.5 API bổ sung

```
PUT    /jobs/{id}/mark               → {mark, pinned, note}
GET    /jobs?mark=interested         → lọc theo mark người dùng gắn

POST   /applications/{id}/events     → {to_status, occurred_at, note}
GET    /applications/{id}/events     → dòng thời gian
GET    /stats                        → phễu + thời gian phản hồi trung bình
```

`GET /stats` là thứ chỉ có được nhờ bảng sự kiện:

```sql
SELECT to_status, count(*) AS n,
       avg(occurred_at - (SELECT occurred_at FROM application_events e2
                          WHERE e2.application_id = e.application_id
                            AND e2.to_status = 'submitted'
                          ORDER BY occurred_at LIMIT 1)) AS avg_delay
FROM application_events e
WHERE to_status <> 'draft'
GROUP BY to_status;
```

### 12.6 Frontend

```
routes/applications/
├── index.tsx       kanban kéo thả — kéo cột sinh application_event
├── $id.tsx         dòng thời gian + giấy tờ + ghi chú + next_action
└── stats.tsx       phễu ứng tuyển, thời gian phản hồi
```

Hai yêu cầu UX:

1. **Kéo thẻ sang cột khác phải hỏi ngày.** Mặc định là hôm nay, nhưng cho sửa —
   lý do ở 12.2.
2. **Nút đánh dấu nằm ngay trên card ở danh sách việc**, không phải chỉ trong trang chi
   tiết. Người dùng duyệt 50 tin một lượt; bắt họ mở từng trang để ẩn một tin là thiết
   kế sai.

---

## 13. Roadmap theo từng vị trí ứng tuyển

Mục 12 lưu **trạng thái đã xảy ra**. Mục này trả lời câu hỏi khác: *bây giờ tôi phải
làm gì tiếp?* Đây là thứ bạn mô tả là "roadmap để người dùng hình dung mình cần chạy
các luồng ra sao cho tới khi được tuyển".

### 13.1 Mô hình: stage có checklist, không phải chỉ là status

```
┌─ 1. ĐÁNH GIÁ ──────────────────────────────────┐
│ ☑ Chấm điểm tự động (Agent 3)                  │
│ ☐ Đọc kỹ tin, xác nhận các gate FLAG           │
│ ☐ Quyết định: ứng tuyển / bỏ qua               │
└───────────────────┬────────────────────────────┘
                    ▼
┌─ 2. CHUẨN BỊ HỒ SƠ ────────────────────────────┐
│ ☐ Sinh CV + Anschreiben (Agent 4)              │
│ ☐ Đọc lại, sửa giọng văn                       │
│ ☐ Kiểm tra PDF: số trang, lớp text ATS         │
│ ☐ Gom giấy tờ kèm theo (bằng, chứng chỉ)       │
│ ⚠ Werkstudent: hỏi công ty về 360h + ngày bắt đầu│
└───────────────────┬────────────────────────────┘
                    ▼
┌─ 3. NỘP ───────────────────────────────────────┐
│ ☐ Nộp qua cổng / email                         │
│ ☐ Ghi ngày nộp thật vào tracker                │
│ ☐ Đặt nhắc hỏi lại sau 14 ngày                 │
└───────────────────┬────────────────────────────┘
                    ▼
┌─ 4. CHỜ PHẢN HỒI ──────────────────────────────┐
│ ☐ Hỏi lại nếu quá 14 ngày                      │
│ ☐ (tuỳ chọn) Tìm người giới thiệu trên LinkedIn│
└───────────────────┬────────────────────────────┘
                    ▼
┌─ 5. PHỎNG VẤN ─────────────────────────────────┐
│ ☑ Sinh bộ prep (Agent 5, tự động)              │
│ ☐ Luyện ≥ 10 câu trong chế độ practice         │
│ ☐ Đọc company brief                            │
│ ☐ Chuẩn bị câu hỏi hỏi lại họ                  │
│ ☐ Kiểm tra thiết bị nếu phỏng vấn online       │
│ ☐ Gửi thư cảm ơn trong 24h sau phỏng vấn       │
└───────────────────┬────────────────────────────┘
                    ▼
┌─ 6. OFFER ─────────────────────────────────────┐
│ ☐ Đối chiếu offer với tin tuyển dụng ban đầu   │
│ ☐ Xác nhận lương và số giờ/tuần                │
│ ☐ Praxisphase: lấy giấy xác nhận ngày bắt đầu  │
│ ☐ Nhắn giảng viên phụ trách để phân Betreuung  │
└────────────────────────────────────────────────┘
```

Stage 6 trong ví dụ trên mang quy tắc riêng của người dùng (Praxisphase, 360 giờ,
báo giảng viên). Đó là lý do checklist phải **sinh từ template cấu hình được**, không
hard-code: người quen của bạn sẽ có ràng buộc khác.

### 13.2 Lược đồ

```sql
-- Template: định nghĩa stage và task mẫu. Có bản mặc định của hệ thống,
-- user clone ra rồi sửa.
CREATE TABLE pipeline_templates (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,  -- NULL = template hệ thống
    name        TEXT NOT NULL,
    stages      JSONB NOT NULL,     -- [{key,label,maps_to_status,tasks:[{label,auto,agent}]}]
    is_default  BOOLEAN NOT NULL DEFAULT false
);

-- Thực thể: task thật của một ứng tuyển cụ thể
CREATE TABLE application_tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id  UUID NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    stage_key       TEXT NOT NULL,          -- 'evaluate' | 'prepare' | 'submit' ...
    label           TEXT NOT NULL,
    done            BOOLEAN NOT NULL DEFAULT false,
    done_at         TIMESTAMPTZ,
    auto            BOOLEAN NOT NULL DEFAULT false,  -- agent tự tick, không phải người
    agent           TEXT,                   -- agent nào hoàn thành task này
    due_at          DATE,
    sort_order      INTEGER NOT NULL,
    note            TEXT
);

CREATE INDEX app_tasks_app ON application_tasks (application_id, stage_key, sort_order);

ALTER TABLE applications
    ADD COLUMN template_id   UUID REFERENCES pipeline_templates(id),
    ADD COLUMN current_stage TEXT NOT NULL DEFAULT 'evaluate';
```

### 13.3 Ba loại task

Phân biệt này quyết định UX:

| Loại | `auto` | Ai tick | Ví dụ |
|---|---|---|---|
| Hệ thống | `true` | Agent, khi xong việc | "Chấm điểm tự động", "Sinh bộ prep" |
| Người dùng | `false` | Người dùng | "Đọc lại, sửa giọng văn", "Nộp hồ sơ" |
| Điều kiện | `false` | Người dùng, **chỉ hiện khi thoả điều kiện** | "Hỏi công ty về 360h" — chỉ hiện nếu `jobs.contract_type = 'working_student'` |

Task điều kiện là chỗ quy tắc riêng của người dùng sống. Biểu diễn trong template:

```json
{
  "label": "Hỏi công ty: ký ngày bắt đầu Praxisphase + xác nhận 360h?",
  "auto": false,
  "when": {"job.contract_type": "working_student"}
}
```

Worker đánh giá `when` khi khởi tạo task, dùng dữ liệu đã có trong `jobs` và
`evaluations` — **không gọi LLM**. Đây lại là một chỗ `WHERE` thắng prompt.

### 13.4 Sinh task và tiến độ

```
Tạo application
  └─ nạp template (user's default hoặc của hệ thống)
     └─ với mỗi stage, với mỗi task:
          nếu có "when" và không thoả → bỏ qua
          ngược lại → INSERT application_tasks
```

Agent tick task của mình khi hoàn thành:

```python
async def mark_auto_task(app_id: UUID, agent: str, label_key: str) -> None:
    await db.execute("""
        UPDATE application_tasks SET done = true, done_at = now()
        WHERE application_id = $1 AND agent = $2 AND NOT done
    """, app_id, agent)
```

Tiến độ tính bằng SQL, không lưu trùng:

```sql
SELECT stage_key,
       count(*) FILTER (WHERE done) AS done,
       count(*)                      AS total
FROM application_tasks
WHERE application_id = $1
GROUP BY stage_key;
```

### 13.5 Quan hệ với status ở mục 12

Hai thứ này **không được đồng bộ tự động hai chiều** — đó là cái bẫy dễ thấy nhất.

- `applications.status` là sự thật về **quan hệ với nhà tuyển dụng** (họ đã mời phỏng
  vấn chưa). Chỉ đổi khi có sự kiện thật, và ghi vào `application_events`.
- `current_stage` là **bạn đang làm gì**. Có thể đi trước status: bạn chuẩn bị hồ sơ
  (stage `prepare`) trong khi status vẫn là `draft`.

Liên kết duy nhất theo một chiều: **status đổi thì stage nhảy tới**, không ngược lại.
Nhà tuyển dụng mời phỏng vấn thì stage lên `interview` dù checklist stage trước còn
dở — và những task chưa xong đó vẫn hiện, được đánh dấu là đã bỏ qua chứ không bị xoá.

### 13.6 API và frontend

```
GET    /applications/{id}/roadmap        → stage, task, tiến độ, việc tiếp theo
PATCH  /application-tasks/{id}           → {done} hoặc {note, due_at}
GET    /pipeline-templates
POST   /pipeline-templates               → clone template hệ thống rồi sửa
```

```
routes/applications/$id/
├── roadmap.tsx     mặc định: stage dọc, checklist từng stage, "việc tiếp theo" nổi bật
├── documents.tsx
├── interview.tsx   bộ prep + chế độ luyện (Agent 5)
└── timeline.tsx    application_events
```

Yêu cầu UX: **luôn hiện đúng một "việc tiếp theo"** ở đầu trang — task chưa xong đầu
tiên của stage hiện tại. Một checklist 25 ô không nói cho người dùng biết phải làm gì
bây giờ; một dòng thì có.

---

## Phụ lục: những quyết định đã có lý do, đừng đảo lại

1. **Lọc bằng SQL, không bằng LLM.** Khoảng cách, ngày tháng, loại hợp đồng, số giờ.
2. **Gate có quyền veto trước khi tính điểm.** Tin fail gate bị loại, không xếp hạng.
3. **`NULL` nghĩa là tin không nêu. Không bao giờ đoán deadline hay số giờ.**
4. **Reviewer trả JSON edit, không trả văn xuôi.**
5. **Mọi câu trong giấy tờ phải truy được về hồ sơ.** Không có ngoại lệ.
6. **Scraper tier personal chỉ chủ sở hữu dùng**, thi hành ở tầng query.
7. **CV compile `lualatex`, Anschreiben compile `xelatex`.** Không đổi.
8. **Lưu nguyên văn tin tuyển dụng.** Không tóm tắt.
9. **Điểm của máy và nhãn của người là hai bảng riêng.** Chấm lại không được ghi đè
   lựa chọn thủ công của người dùng (mục 12.1).
10. **`application_events` chỉ ghi thêm.** `occurred_at` tách khỏi `recorded_at`, vì
    người dùng ghi lại việc đã xảy ra mấy ngày trước (mục 12.2).
11. **Truy xuất hỗn hợp, không vector thuần.** Thuật ngữ chính xác như `VBA` hay
    `Angular 18` cần full-text, không phải embedding (mục 11.4).
12. **`chunks.embed_model` có từ ngày đầu.** Thiếu nó, đổi model embedding sẽ cho kết
    quả vô nghĩa mà không báo lỗi (mục 11.7).
13. **RAG đứng sau bộ lọc SQL, không thay nó.** Lọc cứng trước, xếp hạng ngữ nghĩa sau,
    nếu không sẽ trả về tin rất khớp ý nhưng ở Stuttgart (mục 11.6).
