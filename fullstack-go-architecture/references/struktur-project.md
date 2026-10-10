# Struktur Project Go (Go + HTMX + Alpine.js + MariaDB)

Referensi **lokasi & wiring**: folder, nama file, template engine, `main.go`, sqlc/goose, test, dan langkah menambah domain. Aturan arsitektur (transaksi, pool, timeout, background, auth) **tidak diulang di sini**, rujuk ke `SKILL.md` dengan nomor bagiannya.

## 1. Prinsip Struktur

1. **Domain-based.** Satu fitur = satu package di `internal/<domain>/` berisi handler, service (opsional), form, view, dan task-nya. Tidak ada folder global `handlers/`, `services/`, `models/`.
2. **Perubahan fitur X idealnya hanya menyentuh** `internal/X/`, `queries/X.sql`, satu file migrasi, dua baris registrasi di `cmd/server/main.go`, izinnya di `internal/authz`, dan paling banyak satu `Alpine.data` di `alpine-components.js`. Kalau lebih dari itu, berhenti dan periksa: mungkin ada kode yang seharusnya masuk package platform (§3).
3. **File hanya ditambah saat dibutuhkan.** `service.go`, `tasks.go`, `form.go` itu opsional.

## 2. Root Repository

```
nama-project/
├── cmd/
│   ├── server/main.go         # proses HTTP (§5.1)
│   └── worker/main.go         # proses worker antrean job (§5.2)
├── internal/                  # seluruh kode Go aplikasi (§3)
├── web/                       # layout, komponen global, static asset (§4)
├── queries/                   # input SQL sqlc, satu file per domain
│   └── items.sql
├── migrations/                # goose: 20261009120000_add_items.sql
├── sqlc.yaml
├── go.mod / go.sum
├── Dockerfile                 # multi-stage, hasil akhir 1 binary per cmd
├── docker-compose.yml         # mariadb (≥ 10.6) untuk dev & test
├── .env.example               # nama variabel saja, tanpa nilai rahasia
├── CLAUDE.md / AGENTS.md      # konteks untuk AI assistant
└── docs/
    ├── prd.md
    ├── database.md
    ├── decisions/             # 001-pakai-sqlc.md, ...
    └── guides/                # setup-dev.md, deploy.md
```

Kode aplikasi hanya di `cmd/`, `internal/`, dan `web/`. Dokumentasi hanya di `docs/`. `.env` masuk `.gitignore`.

## 3. `internal/`

```
internal/
├── config/          # Config struct + Load() dari env, validasi, fail fast
├── database/        # Open() *sql.DB + pool (SKILL §6.1), InTx() (SKILL §6.2), IsRetryable() (SKILL §6.4)
├── store/           # HASIL GENERATE sqlc, jangan edit manual
├── render/          # template engine (§4.2)
├── session/         # session di tabel sessions, cookie, WithUser/UserFrom(ctx)
├── middleware/      # requestlog.go (request ID, IP klien, LogHandler), recover, timeout,
│                    #   security.go (header & CSP), auth.go (Require, Perm)
├── authz/           # peran → izin (Perm, Can); tanpa DB
├── audit/           # audit.Write ke tabel audit_log (SKILL §8.4)
├── background/      # Runner goroutine terbatas (SKILL §7.1)
├── jobs/            # kind, payload, Enqueue, Worker (tabel jobs), StatusHandler GET /jobs/{id}
│                    #   + views/_status.html; TIDAK mengimpor domain
├── secret/          # AES-GCM untuk kredensial terenkripsi di DB (SKILL §8)
├── integrations/
│   ├── httpclient.go   # NewClient() (SKILL §9)
│   └── bpjs/           # satu package per provider
├── auth/            # domain login: handler /login & /logout, password.go (Hasher),
│                    #   throttle.go (batas percobaan), views/login.html, _form.html
├── items/           # ===== contoh domain =====
│   ├── handler.go      # type Handler, Routes(mux, auth), method HTTP
│   ├── service.go      # opsional: hanya bila ada business logic / transaksi
│   ├── form.go         # opsional: struct input + Validate()
│   ├── tasks.go        # opsional: handler job milik domain ini
│   ├── views.go        # //go:embed views/*.html
│   ├── views/
│   │   ├── list.html      # full page
│   │   ├── _rows.html     # fragment: loop baris
│   │   ├── _row.html      # fragment: satu baris
│   │   └── _form.html
│   └── handler_test.go
└── orders/          # domain lain, pola sama
```

| Lokasi | Isi | Bukan tempat untuk |
|---|---|---|
| `config/`, `database/`, `render/`, `session/`, `middleware/`, `background/`, `secret/`, `authz/`, `audit/` | Platform lintas domain | Logika bisnis fitur tertentu |
| `store/` | Kode generate sqlc (`*.sql.go`, `models.go`, `db.go`) | File tulisan tangan (akan tertimpa) |
| `jobs/` | Konstanta kind, struct payload, `Enqueue`, `Worker` (kode: `antrean-dan-session.md`) | Logika eksekusi job (taruh di `<domain>/tasks.go`) |
| `integrations/<provider>/` | Client HTTP ke sistem luar | Akses database |
| `<domain>/` | Semua yang spesifik satu fitur | Kode yang dipakai banyak domain (pindah ke platform) |
| `web/` | Layout, komponen global, static | Template milik satu domain |

Arah import yang diizinkan (satu-satunya tempat aturan ini ditulis):

```
cmd/* ──► semua package
<domain> ──► store, database, render, session, middleware, authz, audit, jobs, integrations, background, <domain lain satu arah>
jobs, integrations, platform ──► (tidak mengimpor <domain>)
audit ──► session, middleware, store        middleware ──► session, authz
```

- Kalau `orders` butuh `users`, `orders` mengimpor `users`, tidak sebaliknya. Go menolak import cycle saat compile; kalau terjadi, pindahkan logika bersama ke package ketiga, jangan diakali dengan interface.
- `middleware` tidak boleh mengimpor `audit` (karena `audit` membaca IP & request ID dari `middleware`). Penolakan akses di `Perm` cukup di-log; audit ditulis oleh handler/service.
- `jobs` hanya mengimpor `store`/`database` (dan `render`, `session`, `middleware` untuk `StatusHandler`). Handler eksekusi job ada di `<domain>/tasks.go` dan didaftarkan di `cmd/worker/main.go`, sehingga tidak ada cycle.

## 4. Template & Static

### 4.1 Lokasi & embed

`//go:embed` **tidak bisa** menunjuk ke luar folder package-nya (`..` tidak didukung). Karena itu setiap pemilik template meng-embed miliknya sendiri:

```
web/
├── embed.go
├── layout/base.html
├── components/_pagination.html, _modal.html, _toast.html
└── static/
    ├── css/app.css
    └── js/htmx.min.js, alpine-csp.min.js (@alpinejs/csp), alpine-components.js, app.js
```

```go
// web/embed.go
package web

import "embed"

// Pakai glob *.html: embed sebuah DIREKTORI melewatkan file berawalan "_",
// sehingga fragment seperti _modal.html tidak akan ikut ter-embed.
//
//go:embed layout/*.html components/*.html
var Templates embed.FS

//go:embed static
var Static embed.FS
```

```go
// internal/items/views.go
package items

import "embed"

//go:embed views/*.html
var Views embed.FS
```

### 4.2 Engine (`internal/render`)

Masalah yang diselesaikan:

- `ParseFS` menamai template dengan **nama file saja**, sehingga `_row.html` milik `items` dan `orders` saling menimpa.
- `{{define "content"}}` di `list.html` dan `detail.html` saling menimpa jika di-parse ke satu set.

Solusinya: satu set template per halaman, satu set fragment per domain. Render lewat buffer sesuai SKILL §5 butir 2.

```go
package render

import (
	"bytes"
	"fmt"
	"html/template"
	"io/fs"
	"log/slog"
	"net/http"
	"path"
	"strings"
	"sync"
)

var bufPool = sync.Pool{New: func() any { return new(bytes.Buffer) }}

type Engine struct {
	pages map[string]*template.Template // "items/list"  -> layout + komponen + fragment domain + page
	frags map[string]*template.Template // "items"       -> layout + komponen + fragment domain
	log   *slog.Logger
}

// New dipanggil sekali di main. domains: namespace -> FS yang berisi views/*.html.
func New(layout fs.FS, domains map[string]fs.FS, funcs template.FuncMap, log *slog.Logger) (*Engine, error) {
	base, err := template.New("").Funcs(funcs).ParseFS(layout, "layout/*.html", "components/*.html")
	if err != nil {
		return nil, err
	}
	e := &Engine{pages: map[string]*template.Template{}, frags: map[string]*template.Template{}, log: log}

	for ns, dfs := range domains {
		fragFiles, _ := fs.Glob(dfs, "views/_*.html")
		pageFiles, _ := fs.Glob(dfs, "views/[^_]*.html")

		ft, err := base.Clone()
		if err != nil {
			return nil, err
		}
		if len(fragFiles) > 0 {
			if ft, err = ft.ParseFS(dfs, fragFiles...); err != nil {
				return nil, fmt.Errorf("%s: %w", ns, err)
			}
		}
		e.frags[ns] = ft

		for _, p := range pageFiles {
			pt, err := ft.Clone()
			if err != nil {
				return nil, err
			}
			if pt, err = pt.ParseFS(dfs, p); err != nil {
				return nil, fmt.Errorf("%s: %w", p, err)
			}
			e.pages[ns+"/"+strings.TrimSuffix(path.Base(p), ".html")] = pt
		}
	}
	return e, nil
}

// Render: name "items/list" = full page, "items/_row" = fragment.
// Kalau gagal, log lalu balas 500 (belum ada byte yang terkirim karena memakai buffer).
func (e *Engine) Render(w http.ResponseWriter, r *http.Request, status int, name string, data any) {
	ns, file, _ := strings.Cut(name, "/")
	t, entry := e.pages[name], "base.html"
	if strings.HasPrefix(file, "_") {
		t, entry = e.frags[ns], file+".html"
	}

	buf := bufPool.Get().(*bytes.Buffer)
	buf.Reset()
	defer func() {
		if buf.Cap() <= 1<<20 { // buffer raksasa (laporan besar) jangan ditahan pool
			bufPool.Put(buf)
		}
	}()

	var err error
	if t == nil {
		err = fmt.Errorf("template %q tidak terdaftar", name)
	} else {
		err = t.ExecuteTemplate(buf, entry, data)
	}
	if err != nil {
		e.log.ErrorContext(r.Context(), "render gagal", "template", name, "err", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	buf.WriteTo(w)
}
```

Konvensi isi file:

```html
<!-- web/layout/base.html : entry point semua full page -->
<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <title>{{block "title" .}}App{{end}}</title>
  <!-- syarat lolos CSP (SKILL §5 butir 8) -->
  <meta name="htmx-config" content='{"allowEval":false,"allowScriptTags":false,"includeIndicatorStyles":false}'>
  <link rel="stylesheet" href="/static/css/app.css?v={{version}}">
  <script src="/static/js/htmx.min.js?v={{version}}" defer></script>
  <script src="/static/js/app.js?v={{version}}" defer></script>
  <script src="/static/js/alpine-components.js?v={{version}}" defer></script>
  <script src="/static/js/alpine-csp.min.js?v={{version}}" defer></script>
</head>
<body>{{block "content" .}}{{end}}</body>
</html>
```

```html
<!-- internal/items/views/list.html : hanya define, tanpa <head> -->
{{define "title"}}Items{{end}}
{{define "content"}}
<table><tbody id="item-rows">{{template "_rows.html" .}}</tbody></table>
{{template "_pagination.html" .}}
{{end}}
```

```html
<!-- internal/items/views/_rows.html -->
{{range .Items}}{{template "_row.html" .}}{{end}}
```

- `alpine-components.js` dimuat **sebelum** `alpine-csp.min.js` (keduanya `defer`), supaya listener `alpine:init` sudah terpasang.
- Tidak ada `<script>` inline, `onclick=`, atau `style="..."` di template mana pun (CSP, SKILL §5 butir 8). CSS indikator htmx ada di `app.css` ([auth-dan-keamanan §9](auth-dan-keamanan.md)).
- Nama fragment domain **tidak boleh sama** dengan komponen global (`_pagination.html`, `_modal.html`, `_toast.html`), karena yang di-parse belakangan akan menimpa.
- Fungsi template (`version`, format tanggal/rupiah) didaftarkan sekali di `funcs` saat `render.New`.

### 4.3 Static

```go
static, _ := fs.Sub(web.Static, "static")
mux.Handle("GET /static/", middleware.LongCache(http.StripPrefix("/static/", http.FileServerFS(static))))
```

`LongCache` men-set `Cache-Control: public, max-age=31536000, immutable` **hanya jika** query `v` ada, dan `no-cache` jika tidak ada. Dengan begitu link tanpa versi tidak membuat browser menyimpan file basi selama setahun. Ini aman karena URL dari template selalu membawa `?v=<versi build>` (di-set via `-ldflags "-X main.version=..."`). File di `embed.FS` tidak punya ModTime, jadi tanpa versi di URL browser tidak bisa melakukan revalidasi.

## 5. Wiring

### 5.1 `cmd/server/main.go`

```go
import _ "time/tzdata" // data zona untuk APP_TZ di image distroless/scratch (SKILL §6.1)

func main() {
	if err := run(); err != nil {
		slog.Error("server berhenti", "err", err)
		os.Exit(1)
	}
}

func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	cfg, err := config.Load()
	if err != nil {
		return err
	}
	log := slog.New(middleware.LogHandler(slog.NewJSONHandler(os.Stdout, nil))) // + request_id (SKILL §10)
	slog.SetDefault(log)

	db, err := database.Open(cfg.DB) // APP_TZ, pool, max_statement_time (SKILL §6.1)
	if err != nil {
		return err
	}
	defer db.Close()

	runner := background.New(cfg.BackgroundMax, log)

	view, err := render.New(web.Templates, map[string]fs.FS{
		"auth":   authh.Views, // import authh "app/internal/auth"
		"items":  items.Views, // <- tambah satu baris per domain
		"orders": orders.Views,
		"jobs":   jobs.Views, // fragment _status.html untuk polling
	}, render.Funcs(version), log)
	if err != nil {
		return err
	}

	q := store.New(db)
	sessions := session.NewStore(q, cfg.SessionTTL, cfg.CookieSecure)
	auth := middleware.NewAuth(sessions, log)

	mux := http.NewServeMux()
	authh.NewHandler(db, sessions, authh.NewHasher(), view, log).Routes(mux, auth) // /login, /logout (SKILL §8.1–8.2)
	items.NewHandler(q, view, log).Routes(mux, auth)                              // <- satu baris per domain
	orders.NewHandler(orders.NewService(db, q, log), view, log).Routes(mux, auth)
	jobs.NewStatusHandler(q, view, log).Routes(mux, auth) // GET /jobs/{id} (SKILL §7.3)
	// static (§4.3)

	var ready atomic.Bool // /healthz & /readyz: SKILL §12.5
	ready.Store(true)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), time.Second)
		defer cancel()
		if !ready.Load() || db.PingContext(ctx) != nil {
			http.Error(w, "not ready", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})

	csrf := http.NewCrossOriginProtection()
	for _, o := range cfg.TrustedOrigins { // origin publik bila proxy tidak meneruskan Host (SKILL §8.5)
		if err := csrf.AddTrustedOrigin(o); err != nil {
			return err
		}
	}

	handler := middleware.Chain(mux, // urutan: SKILL §4.3
		middleware.RequestLog(log, cfg.TrustedProxies), // request ID + IP klien (SKILL §8.6)
		middleware.Recover(log),
		middleware.SecurityHeaders(cfg.CookieSecure), // CSP & header; HSTS hanya bila HTTPS
		csrf.Handler,
		middleware.Timeout(cfg.RequestTimeout), // < WriteTimeout, divalidasi config.Load (SKILL §4.4)
	)

	srv := &http.Server{ /* timeout: SKILL §4.1 */ Addr: cfg.Addr, Handler: handler}
	go logPoolStats(ctx, log, db, 30*time.Second) // SKILL §10, fungsi lokal di main.go
	go servePprof(cfg.PprofAddr)                       // listener internal

	errCh := make(chan error, 1)
	go func() { errCh <- srv.ListenAndServe() }()

	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
	}

	// urutan shutdown: SKILL §11
	ready.Store(false)
	time.Sleep(cfg.DrainDelay) // LB melihat /readyz = 503 dan berhenti mengirim request
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		return err
	}
	runner.Wait()
	return nil // defer: db ditutup terakhir
}
```

`middleware.Chain(h, m1, m2, ...)` menerapkan m1 sebagai lapisan paling luar. Satu-satunya `go` mentah yang boleh ada adalah goroutine bootstrap di `main.go` ini. Middleware auth dipasang per route (`Routes(mux, auth)` lalu `auth.Require` atau `auth.Perm(...)` di setiap route), bukan global, supaya `/login`, `/static/`, `/healthz`, `/readyz` tetap publik. Wiring baru ini (auth, health check, drain, `RequestLog`, `SecurityHeaders`) sudah di-compile dan lolos `go vet`.

### 5.2 `cmd/worker/main.go`

Kode lengkapnya ada di [antrean-dan-session.md §5](antrean-dan-session.md): satu baris `w.Handle(kind, ...)` per kind, `w.Every(...)` untuk job periodik, lalu `w.Run(ctx)`. Server dan worker dibangun dari repo yang sama, tetapi merupakan **dua binary dengan pool terpisah**. Server tidak perlu objek worker; ia hanya memanggil `jobs.Enqueue` di dalam `InTx`.

## 6. sqlc & goose

```yaml
# sqlc.yaml
version: "2"
sql:
  - engine: mysql
    schema: migrations        # sqlc membaca anotasi goose (-- +goose Up) dan mengabaikan Down
    queries: queries
    gen:
      go:
        package: store
        out: internal/store
        emit_interface: false # interface hanya bila dibutuhkan test (SKILL §2 butir 4)
```

- Satu sumber schema: **folder `migrations/`**. Tidak ada `schema.sql` terpisah yang bisa beda isinya dengan migrasi.
- Nama query sqlc per domain diberi prefix entitas: `ListItems`, `GetItem`, `CreateOrder`. Ini mencegah tabrakan karena semuanya di-generate ke satu package `store`.
- Migrasi: `goose -dir migrations create add_items sql`. Satu perubahan schema per file, dan jangan edit file yang sudah di-deploy.
- Query sqlc yang dipakai: `:one`, `:many`, `:exec`, `:execrows`, `:execlastid` (insert yang butuh ID), dan `sqlc.slice('ids')` untuk `IN (...)`.
- Durasi relatif terhadap `NOW()` (SKILL §6.3) ditulis `DATE_ADD(NOW(), INTERVAL CAST(sqlc.arg(x) AS SIGNED) SECOND)`. `CAST` membuat sqlc men-generate parameter `int64`, bukan `interface{}`.
- Parser sqlc (dialek MySQL) tidak mengenali sebagian sintaks khusus MariaDB (contoh terverifikasi: `SET STATEMENT ... FOR`). Kalau `sqlc generate` gagal karena itu, ubah pendekatannya (batas waktu query cukup lewat `max_statement_time` di DSN, SKILL §6.3), jangan menulis query manual di luar `store`.
- Setelah mengubah SQL: `sqlc generate`. CI menjalankan `sqlc diff` (atau `sqlc vet`) supaya kode generate tidak tertinggal.

## 7. Konvensi Penamaan

| Hal | Aturan | Contoh |
|---|---|---|
| Package domain | huruf kecil, jamak, tanpa underscore | `items`, `orders`, `orderitems` |
| File di domain | nama tetap | `handler.go`, `service.go`, `form.go`, `tasks.go`, `views.go` |
| Konstruktor | `NewHandler`, `NewService` | `items.NewHandler(q, view, log)` |
| Registrasi route | `func (h *Handler) Routes(mux *http.ServeMux, a *middleware.Auth)` | tiap route dibungkus `a.Perm(authz.X)` atau `a.Require` |
| Izin | konstanta `authz.<Entitas><Aksi>`, nilai `"<entitas>:<aksi>"` | `authz.PatientWrite = "patient:write"` |
| Aksi audit | huruf kecil: `view`, `create`, `update`, `delete`, `export`, `login`, ... | `audit.Entry{Action: "update", Entity: "patient"}` |
| Pattern route | method + path, prefix = nama domain | `"GET /items/{id}"`, `"POST /items"` |
| Nama template | `"<domain>/<file tanpa .html>"` | `"items/list"`, `"items/_row"` |
| Fragment | awalan `_` | `_row.html`, `_form.html` |
| Komponen Alpine | camelCase sesuai domain | `itemTable`, `orderForm` |
| Event HX-Trigger | kebab-case `<entitas>-<aksi>` | `item-saved`, `order-cancelled` |
| Kind job | `"<domain>:<aksi>"`, konstanta `Kind<Domain><Aksi>` | `jobs.KindOrderSync = "order:sync"` |
| Tabel platform | jamak, tanpa prefix domain | `jobs`, `sessions` |
| Query sqlc | `<Verb><Entitas>` | `ListItems`, `UpdateOrderStatus` |

## 8. Test

Go menaruh test **di samping kode** (`handler_test.go`, `service_test.go`), bukan di folder `tests/` yang mirror.

- **Service & query: test ke MariaDB asli** (container dari `docker-compose.yml`, migrasi goose dijalankan di `TestMain`). Jangan mock `store`, karena justru SQL-nya yang perlu diuji.
- **Handler**: `httptest.NewRecorder()`. Cek status, header (`HX-Trigger`, `HX-Redirect`, `Vary`), dan potongan HTML. Uji kedua jalur, full page dan fragment.
- **Template**: satu test yang memanggil `render.New` dengan semua domain. Template rusak akan gagal saat test, bukan saat runtime.
- **Concurrency**: `go test -race ./...` wajib di CI.
- **Izin**: untuk setiap route ber-`Perm`, uji satu role yang boleh (200) dan satu yang tidak (403), plus tanpa session (redirect / 401 + `HX-Redirect`).
- **Audit**: uji bahwa mutasi yang di-rollback tidak meninggalkan baris audit, dan akses baca data sensitif menulis satu baris.
- CRUD polos cukup satu test handler. Prioritaskan service yang punya transaksi dan alur DB → I/O luar → DB.

## 9. Menambah Domain Baru (contoh: `patients`)

1. `goose -dir migrations create add_patients sql` → tulis `CREATE TABLE` + index.
2. `queries/patients.sql` → query bernama `ListPatients`, `GetPatient`, dst. Lalu `sqlc generate`.
3. `internal/patients/views.go` (`//go:embed views/*.html`), `views/list.html`, `views/_rows.html`, `views/_row.html`, `views/_form.html`.
4. `internal/patients/handler.go` dengan `NewHandler` dan `Routes`. Tambah `service.go` **hanya** jika ada business logic atau transaksi multi-query.
5. `cmd/server/main.go`: tambah `"patients": patients.Views` di `render.New` dan satu baris `patients.NewHandler(...).Routes(mux, auth)`. Di `Routes`, setiap route dibungkus `a.Perm(authz.PatientRead)` / `a.Perm(authz.PatientWrite)`; izin baru ditambahkan di `internal/authz` beserta role yang mendapatkannya.
6. Data sensitif: `audit.Write` di dalam `InTx` untuk mutasi dan sebelum render untuk akses baca (SKILL §8.4).
7. Kalau perlu logika Alpine: satu `Alpine.data('patientTable', ...)` di `alpine-components.js`.
8. Kalau ada job: konstanta `Kind...` + struct payload di `internal/jobs/jobs.go`, `jobs.Enqueue` di dalam `InTx` service, handler di `patients/tasks.go`, lalu satu baris `w.Handle(...)` di `cmd/worker/main.go`.
9. Test di `internal/patients/*_test.go`.
10. Kalau ada keputusan arsitektur baru, catat di `docs/decisions/`.

## 10. Anti-Pattern Struktur

Hanya soal lokasi file. Anti-pattern arsitektur (repository pass-through, interface tanpa implementasi kedua, parse template di handler, `*http.Client` di handler, logic Alpine inline) ada di checklist SKILL §13.

- Folder global `handlers/`, `services/`, `models/`, `repositories/`.
- Mengedit file di `internal/store/` secara manual.
- `schema.sql` terpisah dari `migrations/`.
- Package `utils`/`common`/`helpers` serba-ada. Beri nama sesuai fungsinya (`secret`, `render`, `session`).
- Client untuk provider yang sama tersebar di beberapa domain, bukan di `integrations/<provider>/`.
- `<head>`/script disalin ke setiap page, bukan memakai `base.html`.
- `//go:embed views` (direktori) yang diam-diam melewatkan fragment `_*.html`.
- `var tmpl = template.Must(...)` global per file yang melewati `render.Engine`.
- Cek role berupa string di handler (`if user.Role == "admin"`), bukan `a.Perm(...)` / `authz.Can`.
- Membaca `X-Forwarded-For`/`X-Real-IP` di handler, bukan `middleware.ClientIP(ctx)`.
- Script atau style inline di template (melanggar CSP).
