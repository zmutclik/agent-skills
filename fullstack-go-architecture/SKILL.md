---
name: fullstack-go-architecture
description: Panduan arsitektur fullstack Go (net/http + html/template + HTMX + Alpine.js + MariaDB, tanpa Redis). Gunakan saat membuat, me-review, atau refactor HTTP handler, service, transaksi database/sql + sqlc, connection pool, background job (goroutine terbatas / antrean tabel MariaDB), session di database, login/password & batas percobaan, otorisasi per peran, audit log, header keamanan & CSP, zona waktu, rendering template & fragment HTMX, integrasi HTTP pihak ketiga, migrasi & deploy produksi, atau struktur domain project Go.
---

# Fullstack Go Architecture Guide (Go + HTMX + Alpine.js + MariaDB)

Pedoman arsitektur fullstack **Go** + **html/template/HTMX/Alpine.js** + **MariaDB**. MariaDB adalah satu-satunya penyimpan state; Redis **tidak** dipakai secara default (lihat §14).

- **Dokumen ini** = aturan arsitektur & pencegahan bottleneck (DB, pool, timeout, concurrency), keamanan (auth, izin, audit, header), dan deploy.
- **[references/struktur-project.md](references/struktur-project.md)** = apa ditaruh di mana: folder, nama file, template engine, wiring `main.go`, langkah menambah domain. Baca saat membuat file/domain baru.
- **[references/antrean-dan-session.md](references/antrean-dan-session.md)** = kode platform lengkap: antrean job (`internal/jobs`, termasuk endpoint status), `background.Runner`, session (`internal/session`) dan middleware `Require`/`Perm`, beserta migrasi & query sqlc. Baca saat menyentuh package-package itu.
- **[references/auth-dan-keamanan.md](references/auth-dan-keamanan.md)** = kode login (password bcrypt, batas percobaan), otorisasi (`internal/authz`), audit log (`internal/audit`), middleware `RequestLog` (request ID + IP klien) dan `SecurityHeaders`, serta aturan frontend agar lolos CSP. Baca saat menyentuh login, izin, audit, atau header.

Pembagiannya: aturan hanya ada di sini (checklist §13 merangkumnya), lokasi file & arah import di struktur-project, kode platform di antrean-dan-session dan auth-dan-keamanan.

## 1. Tech Stack

| Kebutuhan | Pilihan | Catatan |
|---|---|---|
| Bahasa | Go 1.26+ | `http.CrossOriginProtection` & `sync.WaitGroup.Go` (1.25), dan `golang.org/x/crypto` terbaru mewajibkan 1.26 |
| HTTP | `net/http` (`ServeMux` dengan method & path pattern) | tanpa framework |
| Template | `html/template` + `embed.FS` | auto-escape, parse sekali saat startup |
| Frontend | HTMX 2 + Alpine.js 3 **build CSP** (`@alpinejs/csp`) | supaya lolos CSP tanpa `unsafe-eval` (§5 butir 8) |
| Database | MariaDB **≥ 10.6** via `database/sql` + `github.com/go-sql-driver/mysql` | 10.6+ wajib untuk `SKIP LOCKED` (antrean job) |
| Query | `sqlc` (generate Go dari SQL) | hasil generate = lapisan akses data, tidak perlu repository tambahan |
| Migration | `goose` (file SQL) | `sqlc` membaca folder migrasi yang sama sebagai schema |
| Session | tabel `sessions` (hash token) | §8.5 |
| Password | `golang.org/x/crypto/bcrypt` | satu-satunya library di luar stdlib selain driver DB (§8.1) |
| Job | goroutine terbatas (boleh hilang) / tabel `jobs` + `cmd/worker` (tidak boleh hilang) | §7, pilih per kasus |
| Log | `log/slog` (JSON) | |
| Config | `os.LookupEnv` + validasi saat startup | tanpa library config |

Jangan menambah library di luar tabel ini tanpa alasan konkret (dan catat di `docs/decisions/`).

## 2. Prinsip Utama

1. **Stateless** — tidak ada session, cache, atau counter di memory proses (`map` + `sync.Mutex` global, dsb). Semua shared state (session, antrean job, status job) ada di MariaDB, supaya aplikasi bisa jalan multi-instance di belakang load balancer.
2. **Satu pool per proses, di-inject eksplisit** — satu `*sql.DB` dan satu `*http.Client` per integrasi, dibuat di `main.go`, dioper lewat field struct. Tidak ada variabel global untuk koneksi.
3. **Setiap I/O punya batas waktu** — request, query, transaksi, HTTP keluar, job. Di Go tidak ada timeout default: tanpa deadline, goroutine dan koneksi bisa tertahan selamanya.
4. **Stdlib dulu, abstraksi belakangan** — jangan bikin interface, repository, atau layer "untuk jaga-jaga". Interface kecil didefinisikan di sisi pemakai, dan baru dibuat kalau memang ada implementasi kedua atau kebutuhan test.
5. **Dependency eksternal dibungkus tipis** — payment, BPJS, email, storage cukup satu package di `internal/integrations/<nama>`, supaya ganti provider hanya mengubah satu tempat.

## 3. Layer dalam Satu Domain

```
handler ──► service (opsional) ──► store (sqlc, hasil generate)
                └────────────────► integrations (HTTP luar, tanpa DB)
                └────────────────► jobs (Enqueue = INSERT di transaksi yang sama)
```

| Layer | Tugas | Aturan |
|---|---|---|
| `handler.go` | Parse & validasi input, panggil service/store, render page atau fragment | Tipis. Tidak ada transaksi atau business logic |
| `service.go` | Business logic, **pemilik batas transaksi**, urutan DB → I/O luar → DB | **Lewati** untuk CRUD polos; handler boleh langsung memanggil `*store.Queries` |
| `store` (sqlc) | Query & mutasi murni | Tidak membuat transaksi sendiri. Jangan dibungkus `repository.go` yang hanya meneruskan panggilan |

Arah import (satu jalur, `jobs`/platform tidak mengimpor domain) ada di [struktur-project §3](references/struktur-project.md).

## 4. HTTP Server & Request

### 4.1 Timeout server wajib di-set

`http.Server` dengan nilai nol berarti **tanpa timeout**, sehingga rentan slowloris dan bisa kehabisan file descriptor.

```go
srv := &http.Server{
	Addr:              cfg.Addr,
	Handler:           handler,
	ReadHeaderTimeout: 5 * time.Second,
	ReadTimeout:       15 * time.Second,
	WriteTimeout:      30 * time.Second, // endpoint SSE/stream: atur per-route via http.ResponseController
	IdleTimeout:       60 * time.Second,
}
```

### 4.2 Deadline per request

`WriteTimeout` **tidak** membatalkan `r.Context()`: handler tetap jalan dan tetap memegang koneksi DB. Pasang deadline di middleware supaya semua query di bawahnya ikut terbatas:

```go
func Timeout(d time.Duration) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ctx, cancel := context.WithTimeout(r.Context(), d)
			defer cancel()
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}
```

Route SSE/stream atau upload besar didaftarkan di luar middleware ini (atau dengan durasi sendiri), karena deadline global akan memutusnya. Selalu teruskan `ctx` ke `*Context` method (`QueryContext`, `BeginTx`, `http.NewRequestWithContext`). Jangan pakai `context.Background()` di jalur request.

### 4.3 Batasi body & recover panic

- `r.Body = http.MaxBytesReader(w, r.Body, 1<<20)` sebelum `ParseForm`/decode JSON. Untuk upload file, set limit per route.
- Middleware recover: log stack lalu balas 500. Middleware ini **tidak** menangkap panic di goroutine lain (lihat §7.1).

Urutan middleware (luar → dalam): `RequestLog (request ID, IP klien) → recover → SecurityHeaders → CrossOriginProtection → timeout → mux → Require/Perm (per route)`. Recover dipasang di dalam log supaya request yang panic tetap tercatat dengan `request_id` dan status 500. `SecurityHeaders` di luar CSRF dan timeout supaya respons 403/500 dari lapisan itu tetap membawa header keamanan.

### 4.4 Urutan timeout

Setiap lapisan harus habis **sebelum** lapisan di luarnya. Kalau terbalik, lapisan dalam tetap berjalan (dan memegang koneksi DB) setelah lapisan luar menyerah.

```
HTTP : max_statement_time (10s), http.Client.Timeout  <  RequestTimeout (mis. 20s)  <  WriteTimeout (30s)
Job  : max_statement_time, http.Client.Timeout  <  JobTimeout (60s)  <  LockFor (5m)
       JobTimeout  <  grace period deployment (stop_grace_period / terminationGracePeriodSeconds)
Server: DrainDelay + timeout srv.Shutdown (20s)  <  grace period deployment (§11)
```

`max_statement_time` dan `http.Client.Timeout` sejajar (keduanya lapisan paling dalam), tidak harus saling lebih kecil.

`config.Load` memvalidasi `RequestTimeout < WriteTimeout` dan `JobTimeout < LockFor`, lalu fail fast kalau dilanggar. Integrasi lambat yang tidak muat di `RequestTimeout` dipindah ke `jobs`, bukan dengan menaikkan timeout request.

## 5. Frontend: HTMX + Alpine.js + `html/template`

Detail engine & namespace template ada di [references/struktur-project.md §4](references/struktur-project.md). Aturannya:

1. **Parse sekali saat startup** dari `embed.FS`. Dilarang `template.ParseFiles`/`ParseFS` di dalam handler, karena itu disk I/O plus parsing di setiap request.
2. **Render ke buffer dulu, baru tulis ke `w`.** Kalau template error di tengah, user tidak menerima HTML setengah jadi berstatus 200, dan status/header masih bisa diubah. Header (`Content-Type`, `HX-Trigger`) wajib di-set **sebelum** `WriteHeader`.
3. **Full page vs fragment** — file berawalan `_` adalah fragment. Satu endpoint boleh melayani keduanya:

```go
// isFragment juga men-set Vary supaya cache proxy/browser tidak tertukar antara page dan fragment.
func isFragment(w http.ResponseWriter, r *http.Request) bool {
	w.Header().Add("Vary", "HX-Request, HX-Boosted, HX-History-Restore-Request")
	// hx-boost dan restore history (tombol Back saat cache htmx kosong) juga mengirim
	// HX-Request: true, padahal keduanya butuh halaman penuh.
	return r.Header.Get("HX-Request") == "true" &&
		r.Header.Get("HX-Boosted") != "true" &&
		r.Header.Get("HX-History-Restore-Request") != "true"
}

func (h *Handler) List(w http.ResponseWriter, r *http.Request) {
	items, err := h.q.ListItems(r.Context())
	if err != nil {
		h.fail(w, r, err)
		return
	}
	name := "items/list"
	if isFragment(w, r) {
		name = "items/_rows"
	}
	h.view.Render(w, r, http.StatusOK, name, map[string]any{"Items": items})
}
```

4. **Server → Alpine lewat header `HX-Trigger`**, bukan `<script>` di dalam fragment. Pakai nama event **kebab-case**, karena atribut HTML di-lowercase sehingga `@itemSaved` tidak akan pernah cocok:

```go
trigger, _ := json.Marshal(map[string]any{"item-saved": map[string]any{"id": item.ID}})
w.Header().Set("HX-Trigger", string(trigger)) // sebelum Render/WriteHeader
h.view.Render(w, r, http.StatusCreated, "items/_row", item)
```

```html
<div x-data="itemTable" @item-saved.window="onSaved($event.detail)">...</div>
```

5. **Error validasi (400/422) dan login diblokir (429) tetap di-swap** — taruh di `static/js/app.js` (file statis, bukan inline):

```js
document.addEventListener('htmx:beforeSwap', (evt) => {
  const s = evt.detail.xhr.status;
  if (s === 400 || s === 422 || s === 429) { evt.detail.shouldSwap = true; evt.detail.isError = false; }
});
```

6. **Logic Alpine di `static/js/alpine-components.js`** sebagai `Alpine.data('nama', ...)`. Inline `x-data` hanya untuk toggle sederhana, dalam batas ekspresi build CSP (butir 8).
7. **Static asset** di-embed ke binary dan di-serve dengan `Cache-Control` panjang + query versi (`/static/app.js?v={{version}}`). Reverse proxy/CDN boleh meng-cache di depannya, tapi sumber kebenarannya tetap binary, bukan folder terpisah di server.
8. **Lolos Content-Security-Policy tanpa `'unsafe-inline'`/`'unsafe-eval'`** (header-nya dipasang `SecurityHeaders`, §8.6). Dengan CSP ini, HTML hasil injeksi tidak bisa menjalankan script. Konsekuensinya:
   - Alpine memakai **build CSP** (`@alpinejs/csp`). Ekspresi sederhana di atribut tetap boleh (`x-data="{ open: false }"`, `@click="open = !open"`, `x-show="count > 3"`), tapi arrow function, template literal, akses global (`document`, `Math`, `console`), dan `x-html` berisi input user tidak boleh.
   - htmx dikonfigurasi `allowEval:false`, `allowScriptTags:false`, `includeIndicatorStyles:false` lewat `<meta name="htmx-config">`, sehingga `hx-on:*`, filter event (`hx-trigger="click[ctrlKey]"`), dan `hx-vals='js:...'` tidak dipakai.
   - Tidak ada `<script>` inline, atribut `onclick=`, atau `style="..."` di template. Semuanya lewat file di `/static` dan class CSS.

   Tabel pengganti dan CSS indikator htmx ada di [auth-dan-keamanan §9](references/auth-dan-keamanan.md). Putuskan ini sejak awal, karena memasang CSP ketat di aplikasi yang sudah penuh script inline jauh lebih mahal.

## 6. Database (MariaDB) — Sumber Bottleneck Utama

### 6.1 Pool `*sql.DB`

```go
// internal/database/database.go
type Config struct {
	DSN              string         // user:pass@tcp(host:3306)/nama_db, parameter wajib diisi Open
	Location         *time.Location // APP_TZ; sama untuk Go (loc) dan sesi MariaDB (time_zone)
	MaxOpen          int
	MaxLifetime      time.Duration
	StatementTimeout time.Duration // max_statement_time, lapisan timeout terdalam (§4.4)
}

func Open(cfg Config) (*sql.DB, error) {
	mc, err := mysql.ParseDSN(cfg.DSN)
	if err != nil {
		return nil, err
	}
	mc.ParseTime = true
	mc.Loc = cfg.Location // DATETIME dibaca sebagai waktu di zona ini
	mc.Timeout = 5 * time.Second
	mc.ReadTimeout = 30 * time.Second
	mc.WriteTimeout = 30 * time.Second
	if mc.Params == nil {
		mc.Params = map[string]string{}
	}
	// Driver menjalankan SET untuk setiap Params saat koneksi dibuka: NOW() dan nilai
	// DEFAULT CURRENT_TIMESTAMP memakai zona yang sama dengan mc.Loc.
	mc.Params["time_zone"] = tzOffset(cfg.Location)
	mc.Params["max_statement_time"] = strconv.FormatFloat(cfg.StatementTimeout.Seconds(), 'f', -1, 64)

	db, err := sql.Open("mysql", mc.FormatDSN())
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(cfg.MaxOpen)        // dari env, lihat rumus di bawah
	db.SetMaxIdleConns(cfg.MaxOpen)        // = MaxOpen, supaya tidak buka-tutup koneksi saat beban naik
	db.SetConnMaxLifetime(cfg.MaxLifetime) // < wait_timeout MariaDB & idle timeout proxy/LB
	db.SetConnMaxIdleTime(5 * time.Minute)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return db, db.PingContext(ctx)
}

// tzOffset: offset tetap ('+07:00') tidak butuh tabel zona waktu MariaDB.
// Hanya benar untuk zona tanpa DST (seluruh WIB/WITA/WIT); zona ber-DST pakai UTC.
func tzOffset(loc *time.Location) string {
	_, off := time.Now().In(loc).Zone()
	sign := '+'
	if off < 0 {
		sign, off = '-', -off
	}
	return fmt.Sprintf("'%c%02d:%02d'", sign, off/3600, off%3600/60)
}
```

`database.Config` diisi `config.Load` dari env (`DB_DSN` tanpa parameter, `APP_TZ`, `DB_MAX_OPEN`, dst.). Parameter driver yang wajib (`parseTime`, timeout, `time_zone`, `max_statement_time`) di-set oleh `Open`, bukan ditulis tangan di DSN, supaya tidak ada environment yang lupa salah satunya.

**Zona waktu.** Go dan sesi MariaDB memakai **satu zona dari `APP_TZ`** (mis. `Asia/Jakarta`), bukan zona container atau zona server DB. `Open` men-set keduanya: `Loc` driver (cara `DATETIME` dibaca ke `time.Time`) dan `time_zone` sesi (nilai `NOW()` dan `DEFAULT CURRENT_TIMESTAMP`). Dengan DSN lama `loc=Local`, container UTC yang membaca DB berzona WIB mendapat waktu bergeser 7 jam (terbukti di uji).

- `config.Load` memanggil `time.LoadLocation(APP_TZ)`, dan `main.go` meng-import `_ "time/tzdata"` supaya image distroless/scratch tetap punya data zona.
- Offset tetap (`'+07:00'`) dipakai supaya tidak butuh tabel zona waktu MariaDB. Ini benar untuk WIB/WITA/WIT karena tanpa DST. Aplikasi lintas zona yang punya DST memakai `UTC`, lalu dikonversi saat ditampilkan.
- Laporan per hari (`DATE(created_at)`) mengikuti zona ini. Mengganti zona setelah ada data tidak mengubah data lama, jadi tentukan sekali di awal.

**Rumus anggaran koneksi** (jangan hardcode angka):

```
MaxOpen_server × jumlah_instance_server
+ MaxOpen_worker × jumlah_instance_worker
+ cadangan (admin, cron, migrasi)          ≤  ~80% max_connections MariaDB
```

Cek dengan `SHOW VARIABLES LIKE 'max_connections';` dan `SHOW STATUS LIKE 'Threads_connected';`.

**`database/sql` tidak punya `pool_timeout`.** Saat pool penuh, `QueryContext` menunggu sampai `ctx` habis. Tanpa deadline (§4.2), request akan antre selamanya. Indikator pool habis: `db.Stats().WaitCount` dan `WaitDuration` yang terus naik (lihat §10).

### 6.2 Transaksi pendek, I/O luar di luar transaksi

Batas transaksi hanya di service, lewat satu helper (`internal/database/tx.go`):

```go
func InTx(ctx context.Context, db *sql.DB, fn func(q *store.Queries) error) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback() // no-op setelah Commit sukses
	if err := fn(store.New(tx)); err != nil {
		return err
	}
	return tx.Commit()
}
```

```go
func (s *Service) CreateOrder(ctx context.Context, in CreateOrderInput) (int64, error) {
	var orderID int64
	err := database.InTx(ctx, s.db, func(q *store.Queries) error {
		var err error
		// query sqlc ":execlastid", sync_status default 'pending'
		if orderID, err = q.InsertOrder(ctx, in.toParams()); err != nil {
			return err
		}
		if err := q.DeductStock(ctx, in.stockParams()); err != nil {
			return err
		}
		// Enqueue = INSERT ke tabel jobs lewat q yang sama: order dan job-nya
		// commit bersama atau rollback bersama. Tidak ada job yang hilang.
		_, err = jobs.Enqueue(ctx, q, jobs.KindOrderSync, jobs.OrderSync{OrderID: orderID})
		return err
	})
	// sync ke BPJS (I/O luar) dikerjakan worker, DI LUAR transaksi ini
	return orderID, err
}
```

Aturan yang tidak boleh dilanggar:

- **Dilarang HTTP call, kirim email, atau `time.Sleep` di dalam `fn`.** Transaksi menahan koneksi dan row lock selama menunggu, pool cepat habis, dan endpoint lain yang cuma `SELECT` ringan ikut macet. Pekerjaan yang butuh I/O luar dititipkan lewat `jobs.Enqueue` (hanya `INSERT`, cepat) di dalam `fn`.
- **Dilarang memakai `s.db` atau `s.q` (di luar tx) di dalam `fn`.** Satu goroutine yang memegang tx lalu meminta koneksi kedua akan **macet sampai `ctx` timeout** saat pool penuh, dan saat beban tinggi semua request kena. Semua query di dalam transaksi lewat `q` dari parameter.
- Kalau butuh data dari API luar sebelum menulis: panggil API dulu, baru buka transaksi. Kalau butuh hasil API setelah menulis: transaksi pendek → API → transaksi pendek kedua.

### 6.3 Query

- **Index** untuk kolom filter/join/order. Cek dengan `EXPLAIN` sebelum query masuk `queries/*.sql`.
- **Hindari N+1** — sqlc tidak punya lazy-load, jadi N+1 muncul sebagai query di dalam loop Go. Ganti dengan `JOIN` atau satu query `WHERE id IN (sqlc.slice('ids'))`.
- **Pagination** dengan `LIMIT` + keyset (`WHERE id < ?`) untuk tabel besar. `OFFSET` besar akan memindai banyak baris.
- **Batas waktu di sisi server**: membatalkan `ctx` hanya memutus koneksi di sisi Go, sedangkan query bisa tetap jalan di MariaDB. Tambahkan `&max_statement_time=10` di DSN (driver menjalankan `SET` saat koneksi dibuka) sebagai pengaman untuk seluruh pool. Kalau ada laporan berat yang butuh waktu lebih lama, buat pool kedua dengan nilai lebih besar. Jangan pakai `SET STATEMENT ... FOR` per query (tidak dikenali sqlc, lihat struktur-project §6).
- **Waktu dihitung di SQL dengan `NOW()`**, bukan `time.Now()` dari Go, untuk semua perbandingan seperti expiry, `run_at`, dan lock (pola sqlc-nya di struktur-project §6). Dengan begitu tidak ada selisih jam antara server aplikasi dan DB, dan zona `NOW()` sama dengan zona Go (§6.1).
- Aktifkan **slow query log** dan periksa secara berkala.

### 6.4 Lock contention

- Transaksi yang lama terbuka menahan row lock. Cek dengan `SELECT * FROM information_schema.INNODB_TRX ORDER BY trx_started;`.
- `SELECT ... FOR UPDATE` selalu dalam urutan yang sama (mis. urut ID) supaya tidak deadlock.
- **`UPDATE`/`DELETE` massal pada tabel yang ramai ditulis: cari ID dulu, lalu ubah lewat PK.** `UPDATE/DELETE ... WHERE <kondisi range>` dicari lewat secondary index, jadi mengunci *index → PK*, termasuk record pertama **sesudah** range karena batas range baru dicek setelah lock diambil. Sementara itu, `UPDATE ... WHERE id = ?` biasa mengunci *PK → index*. Urutan yang berlawanan ini menghasilkan deadlock, baik di REPEATABLE READ maupun READ COMMITTED, dan index tambahan tidak menolong. Polanya: `SELECT id ... LIMIT 1000` (tanpa lock), lalu `UPDATE/DELETE ... WHERE id IN (sqlc.slice('ids')) AND <kondisi diulang>`. Contohnya `ReclaimJobs` dan `DeleteJobs` di [antrean-dan-session §2](references/antrean-dan-session.md).
- Deadlock (1213) dan lock wait timeout (1205) boleh di-retry 1–2 kali dengan mengulang **seluruh** `InTx`, bukan hanya query terakhir, karena transaksinya sudah di-rollback oleh MariaDB. Cek dengan helper di `internal/database`:

```go
func IsRetryable(err error) bool {
	var me *mysql.MySQLError
	return errors.As(err, &me) && (me.Number == 1213 || me.Number == 1205)
}
```
- Jangan gabungkan update yang tidak berkaitan ke satu transaksi besar.

### 6.5 Tabel FEDERATED / FEDERATEDX

Tabel FEDERATED ke server MariaDB lain (billing/master) tidak mendukung pooling ke remote dengan baik, praktis satu query = satu koneksi remote. Ini titik macet yang tidak bisa diperbaiki dari kode Go. Kalau sering dibaca, sinkronkan berkala ke tabel lokal (job periodik, `worker.Every`, §7.2) dan jangan di-query live di setiap request.

### 6.6 Cache

Default-nya **tanpa cache**. Urutan sebelum menambahkannya:

1. Perbaiki query dulu: index, hilangkan N+1, kurangi kolom yang dibaca. Ini yang paling sering jadi masalah.
2. Static asset sudah di-cache browser (§5 butir 7).
3. Kalau metrik (§10) membuktikan data referensi yang jarang berubah (master tarif, poli, wilayah) masih menjadi beban, boleh **cache in-process read-only** dengan syarat: TTL pendek (≤ 5 menit), ukuran terbatas, dan basi selama TTL bisa diterima karena tiap instance punya salinan sendiri. Cegah cache stampede dengan `golang.org/x/sync/singleflight` per key. Ini bukan "state" dalam arti §2 butir 1 karena kebenarannya tetap di MariaDB.
4. Data yang harus konsisten antar instance (session, kuota, counter, lock) **tidak boleh** di-cache in-process.
5. Read replica baru dipertimbangkan setelah metrik menunjukkan beban baca memang dominan.

## 7. Background Work

| Kasus | Pakai | Kalau gagal/restart |
|---|---|---|
| Ringan, boleh hilang (hitung statistik, warm cache, notifikasi non-kritis) | `background.Runner` (goroutine terbatas) | hilang, cukup di-log |
| Berat, harus retry, tidak boleh hilang (email, PDF, sync BPJS/API luar) | `jobs.Enqueue` + proses `cmd/worker` | retry dengan backoff, reclaim kalau worker mati |

Jangan campur: kalau ragu, pakai `jobs`. Audit log **bukan** keduanya: ditulis sinkron di request (§8.4).

### 7.1 Goroutine in-process harus terbatas

`go func()` bebas di handler adalah bottleneck tersembunyi: jumlahnya tak terbatas saat beban tinggi, panic-nya mematikan **seluruh proses** (middleware recover tidak menangkapnya), dan task yang sedang jalan hilang saat shutdown. Semua goroutine background lewat satu `background.Runner` (kode: [antrean-dan-session §6](references/antrean-dan-session.md)):

```go
ok := h.runner.Go(r.Context(), "audit-log", 5*time.Second, func(ctx context.Context) {
	// ctx: membawa request ID, tidak ikut batal saat request selesai, dibatasi timeout sendiri
})
// ok == false: runner penuh, task dilewati (tidak memblokir request)
```

- Kapasitas tetap (`BackgroundMax`). Kalau penuh, task **ditolak**, bukan antre, karena pekerjaan di sini memang boleh hilang.
- Panic di dalam task di-recover dan di-log, dan `runner.Wait()` dipanggil saat shutdown (§11).
- Oper **ID**, bukan `*sql.Tx` atau struct yang masih dipakai handler. Goroutine memakai `*sql.DB` bersama, bukan transaksi milik request.

### 7.2 Antrean job di MariaDB (`internal/jobs`)

Tabel `jobs` + proses `cmd/worker`. Implementasi lengkap (migrasi, query, `Worker`) ada di [references/antrean-dan-session.md](references/antrean-dan-session.md). Cara kerjanya:

```
service: InTx { tulis data bisnis; jobs.Enqueue(q, kind, payload) }   ← satu commit
worker : InTx { SELECT ... FOR UPDATE SKIP LOCKED LIMIT slot_kosong; UPDATE status='running' }  ← transaksi pendek
         handler(ctx, payload) di LUAR transaksi, dengan JobTimeout; slot kosong langsung diisi lagi
         sukses → done | gagal → pending + backoff | permanen / attempt habis → failed (deadlock → retry)
tiap menit (semua instance, idempoten): reclaim 'running' yang lewat LockFor (→ failed kalau attempt habis),
         jadwalkan job periodik
job:cleanup (periodik, satu instance per slot): hapus job done/failed lewat KeepDays, per 1000 ID sampai habis
```

Aturan:

- **Enqueue selalu lewat `q` dari `InTx`**, bersama data bisnisnya. Kalau tidak ada transaksi bisnis, panggil `jobs.Enqueue(ctx, store.New(db), ...)` saja. Jangan enqueue setelah commit, karena kalau proses mati di antaranya, job hilang.
- **Handler wajib idempoten.** Job bisa jalan lebih dari sekali (retry, atau worker mati setelah handler sukses tapi sebelum status `done` tersimpan). Cek status di DB sebelum memanggil API luar. Aturan transaksi §6.2 juga berlaku di dalam handler.
- **Handler menghormati `ctx`** (`JobTimeout`, urutannya di §4.4). Kalau handler mengabaikan ctx dan berjalan melewati `LockFor`, job di-reclaim dan dijalankan dua kali.
- **Error permanen** (payload rusak, data sudah tidak valid): `return fmt.Errorf("...: %w", jobs.ErrPermanent)`, job langsung `failed` tanpa retry. Job `failed` wajib dipantau (§10); query untuk menjalankannya ulang ada di [antrean-dan-session §8](references/antrean-dan-session.md).
- **Worker = proses terpisah** dengan pool sendiri: `MaxOpen_worker ≥ Concurrency + 2` (satu untuk loop claim/maintenance, satu cadangan), dan masuk hitungan anggaran §6.1. Job berat tidak merebut koneksi milik HTTP.
- **Job periodik** (sinkron FEDERATED, hapus session kedaluwarsa) lewat `worker.Every(kind, interval)`, dengan resolusi 1 menit. Aman dijalankan multi-instance karena `unique_key` per slot waktu, sehingga hanya satu instance yang berhasil `INSERT IGNORE`. Tidak perlu cron terpisah atau `GET_LOCK`.
- **Latensi & kapasitas**: saat antrean kosong, job baru diambil paling lambat `PollInterval` (mis. 1 detik); kalau butuh respons instan, kerjakan di request. Saat antrean penuh, slot yang selesai langsung diisi lagi tanpa menunggu poll. Polling `SKIP LOCKED` memakai index `(status, run_at)` dan tidak memblokir `INSERT` job baru (sudah diuji). Ini nyaman untuk ratusan sampai ribuan job per menit. Kalau jauh di atas itu, lihat §14.
- **Tabel `jobs` hanya diubah lewat PK** (§6.4). Reclaim dan penghapusan job lama memakai pola "SELECT id → UPDATE/DELETE by id". Penghapusan dijalankan per 1000 ID oleh satu job periodik, bukan oleh setiap instance tiap menit. Tanpa pola ini, `MarkJobDone` deadlock dengan reclaim/cleanup, status gagal tersimpan, dan job dijalankan dua kali setelah `LockFor` (sudah terbukti di uji).
- **Status job yang gagal disimpan karena deadlock di-retry** (maks 3 kali). Claim yang bentrok cukup diulang, karena transaksinya sudah rollback dan job tetap `pending`.

### 7.3 Polling status job dari HTMX

- `jobs.Enqueue` mengembalikan ID job. Kirim ID itu ke fragment (`hx-get="/jobs/{id}" hx-trigger="every 2s"`); endpoint status cukup satu lookup PK (`GetJobStatus`).
- Balas **HTTP 286** untuk menghentikan polling HTMX, baik saat job `done`, `failed`, **maupun saat job tidak ditemukan**. Kalau tidak, polling berjalan terus.
- Job yang statusnya dipolling user di-enqueue dengan `jobs.EnqueueFor(ctx, q, userID, kind, payload)`. `StatusHandler` hanya menampilkan job milik user yang login; job orang lain atau job sistem dijawab 286 `not_found`, jadi ID yang ditebak tidak membocorkan apa pun.
- Kode `jobs.StatusHandler`: [antrean-dan-session §3](references/antrean-dan-session.md).

## 8. Auth, Session & Secret

| Kasus | Pendekatan |
|---|---|
| UI html/template same-origin (default) | Token acak di cookie `HttpOnly; Secure; SameSite=Lax`; tabel `sessions` hanya menyimpan **SHA-256 token** + `user_id` + `expires_at` |
| Client proxy Catalog (SIMRS / pendaftaran online) | Bearer token acak (`crypto/rand`, ≥32 byte). Simpan **hanya SHA-256 hash** + scope modul + expiry + status cabut. Lookup by hash. Bukan session/JWT user |
| SPA/mobile beda domain | JWT access pendek + refresh token, hanya jika memang ada client seperti ini |
| Service internal trusted | API key atau OAuth2 client credentials bila butuh scope |

Identity provider (Keycloak, dsb.) baru dipakai kalau butuh SSO, multi-tenant dengan role kompleks, atau compliance.

Kode untuk §8.1–8.4 dan §8.6 ada di [references/auth-dan-keamanan.md](references/auth-dan-keamanan.md); session dan middleware `Require`/`Perm` di [antrean-dan-session §7](references/antrean-dan-session.md).

### 8.1 Password

- **bcrypt** (`golang.org/x/crypto/bcrypt`) dengan cost 12 (±0,2 detik per verifikasi). Jangan SHA-256/MD5 polos, jangan password terenkripsi yang bisa dibalik, jangan skema hashing buatan sendiri.
- Panjang minimal 15 karakter bila password adalah satu-satunya faktor (NIST SP 800-63B-4; 8 bila ada MFA). Maksimal 72 byte karena itu batas bcrypt; password lebih panjang **ditolak**, bukan dipotong diam-diam. Tidak ada aturan komposisi (wajib angka/simbol); lebih baik tolak password yang umum atau pernah bocor.
- **Jumlah bcrypt paralel dibatasi** (`auth.Hasher`, slot = jumlah CPU, menunggu slot mengikuti ctx request). Tanpa batas, banjir `POST /login` menghabiskan CPU dan semua endpoint lain ikut lambat.
- Username yang tidak dikenal tetap menjalankan bcrypt (dengan hash dummy) dan mendapat pesan yang sama dengan password salah, supaya username terdaftar tidak bisa ditebak dari pesan maupun waktu respons.
- Hash ber-cost lama di-upgrade saat login sukses (`auth.NeedsRehash`).

### 8.2 Batas percobaan login

Tidak butuh Redis: cukup tabel `login_failures` + `COUNT` ber-index dalam jendela geser 15 menit.

- Ada dua kunci: **per username** (5 kali gagal) dan **per IP** (30 kali gagal, sengaja longgar karena satu klinik bisa keluar lewat satu IP NAT). Kalau salah satunya terlewati, balas 429 **tanpa** menjalankan bcrypt.
- Login sukses menghapus kegagalan milik username saja, bukan milik IP. Kalau ikut dihapus, penyerang yang punya satu akun valid bisa me-reset batas IP-nya.
- Orang lain bisa memakai blokir per username untuk mengunci akun target selama 15 menit. Ini trade-off yang diterima. Jangan ubah menjadi kunci permanen yang hanya bisa dibuka admin.
- IP klien hanya diambil dari `middleware.ClientIP(ctx)` (§8.6). Tanpa pembacaan `X-Forwarded-For` yang benar, semua user terlihat ber-IP proxy sehingga batas per IP mengunci semua orang, atau penyerang memalsukan header untuk lolos dari batas.
- Kegagalan lama dihapus job periodik (`jobs.KindLoginFailureCleanup`).

### 8.3 Otorisasi

- **Izin dicek per route**, bukan di dalam handler: `a.Perm(authz.PatientWrite)(handler)`. Route yang cukup butuh login memakai `a.Require`. Dengan begitu satu baris `Routes` sudah memperlihatkan siapa boleh memanggil apa.
- Peran → izin didefinisikan di kode (`internal/authz`), bukan di tabel, supaya perubahan hak akses lewat review dan ikut versi aplikasi. Deny by default: role yang tidak terdaftar tidak punya izin apa pun.
- Role dan status aktif dibaca bersama session dalam satu query, jadi perubahan role dan penonaktifan user berlaku di request berikutnya.
- Izin route **tidak** menggantikan pembatasan data. Kalau user hanya boleh melihat data unitnya, filter itu ditulis di query (`WHERE unit_id = ?`, `owner_id = ?`) dengan nilai dari `session.UserFrom(ctx)`, bukan dari parameter request.
- Kalau satu user butuh beberapa role, ganti kolom `users.role` dengan tabel `user_roles`. `authz.Can` tetap jadi satu-satunya tempat pengecekan.

### 8.4 Audit log

Untuk data sensitif (rekam medis, data pribadi, keuangan), audit log adalah **data, bukan log**. Ia tidak boleh hilang.

- Ditulis **sinkron** dengan `audit.Write`. Jangan lewat `background.Runner` (boleh hilang, §7.1) dan jangan mengandalkan `slog`.
- Mutasi: `audit.Write(ctx, q, ...)` dengan `q` dari `InTx` yang sama, sehingga audit ikut commit atau rollback bersama datanya.
- Akses baca data sensitif (buka detail pasien, ekspor): tulis audit **sebelum** render. Kalau audit gagal tercatat, balas 500; data tidak ditampilkan tanpa jejak.
- Isinya: siapa (`user_id`), apa (`action`, `entity`, `entity_id`), dari mana (IP, `request_id`), dan kapan. `detail` hanya berisi ID dan nama field yang berubah, **bukan** isi data medis atau password.
- Login, login gagal, dan logout ikut diaudit.
- Append-only: user DB aplikasi hanya punya `SELECT, INSERT` di `audit_log` (§12.2). Retensi dan pengarsipan mengikuti regulasi yang berlaku (konfirmasi ke tim kepatuhan), dan dijalankan oleh DBA, bukan aplikasi.

### 8.5 Session

- **Token hanya di cookie, DB hanya menyimpan hash-nya.** Kalau tabel `sessions` bocor, isinya tidak bisa dipakai untuk login.
- **Rotasi session saat login** (hapus baris lama) untuk mencegah session fixation, dan hapus baris saat logout. "Logout semua perangkat" cukup `DELETE ... WHERE user_id = ?`.
- **1 lookup PK per request, tanpa UPDATE per request.** Expiry diperpanjang paling sering tiap 5 menit (cek `idle_seconds` dari query), supaya tidak ada write ke DB di setiap klik. Session kedaluwarsa dihapus oleh job periodik.
- **Cookie session tanpa `MaxAge`/`Expires`.** Masa berlaku hanya ditentukan `expires_at` di DB. Kalau cookie diberi `MaxAge=TTL`, cookie habis pada `login + TTL` walaupun session di DB sudah diperpanjang, sehingga user yang aktif tetap ter-logout.
- **CSRF**: bungkus mux dengan `http.CrossOriginProtection` (Go 1.25). Request browser lintas origin yang bukan GET/HEAD/OPTIONS ditolak, sedangkan client non-browser (bearer M2M) tidak terpengaruh. Untuk browser yang tidak mengirim `Sec-Fetch-Site`, pengecekan beralih ke `Origin` vs `Host`, jadi reverse proxy wajib meneruskan `Host` asli (`proxy_set_header Host $host;`) atau origin publik didaftarkan lewat `AddTrustedOrigin`. Kalau tidak, POST yang sah ditolak 403.
- Bedakan "belum login" dari "DB bermasalah": `sql.ErrNoRows` → `session.ErrNoSession` → redirect login, error lain → 500. Jangan semua error diarahkan ke login.
- Request HTMX tidak boleh di-`http.Redirect` ke halaman login, karena halaman login akan ter-swap ke dalam fragment. Pakai header `HX-Redirect` + status 401. Kode `middleware.Auth.Require`: [antrean-dan-session §7](references/antrean-dan-session.md).
- **User nonaktif langsung kehilangan session**, karena `GetSession` join ke `users ... is_active`. Setelah ganti password atau menonaktifkan user, panggil `DeleteUserSessions` supaya semua perangkat ter-logout.
- Di HTTPS, nama cookie memakai prefix `__Host-` (`__Host-session_id`). Browser menolak cookie ber-prefix ini kalau tanpa `Secure` atau kalau memakai `Domain`, sehingga subdomain lain tidak bisa menimpanya.
- Respons ber-session diberi `Cache-Control: no-store` oleh `Require`, supaya data tidak tersimpan di cache browser atau proxy (misalnya tombol Back setelah logout di komputer loket bersama).

### 8.6 Header keamanan & IP klien

- `middleware.SecurityHeaders` dipasang global: CSP ketat (§5 butir 8), `X-Content-Type-Options: nosniff`, `frame-ancestors 'none'` + `X-Frame-Options: DENY`, `Referrer-Policy`, `Cross-Origin-Opener-Policy`, dan HSTS **hanya** bila aplikasi diakses lewat HTTPS.
- `middleware.RequestLog` (lapisan terluar) membuat request ID dan menentukan IP klien sekali per request. Handler dan audit membacanya lewat `middleware.ClientIP(ctx)` / `middleware.RequestID(ctx)`. Jangan membaca `X-Forwarded-For` atau `X-Real-IP` langsung di handler.
- `X-Forwarded-For` hanya dipercaya kalau koneksi datang dari proxy di `TRUSTED_PROXIES`, dan dibaca **dari kanan**. Entri paling kiri bisa diisi bebas oleh klien.

### 8.7 Config & secret

- Konfigurasi deployment dari environment variable (DB DSN, `SECRET_KEY`, pool size, timeout, cookie setting, retensi, bootstrap admin sekali pakai). Divalidasi di startup; kalau tidak valid, proses langsung exit (fail fast).
- Wajib ada: `APP_TZ` (§6.1), `TRUSTED_PROXIES` (CIDR reverse proxy sendiri; kosong = tidak ada proxy), dan `COOKIE_SECURE`. Kalau `APP_ENV=production`, `config.Load` menolak start bila `COOKIE_SECURE` bukan `true`.
- **Kredensial BPJS operasional tidak disimpan di `.env`**, tapi terenkripsi per environment di MariaDB (AES-GCM, `crypto/cipher`). `SECRET_KEY` untuk enkripsi/dekripsinya hanya ada di environment.

## 9. Integrasi HTTP Pihak Ketiga

`http.DefaultClient` **tidak punya timeout**, dan `MaxIdleConnsPerHost` default-nya **2**. Akibatnya, call paralel ke host yang sama (BPJS, payment) terus membuka koneksi TLS baru. Satu client per integrasi, dibuat sekali:

```go
func NewClient(timeout time.Duration, maxPerHost int) *http.Client {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.MaxIdleConnsPerHost = maxPerHost
	t.MaxConnsPerHost = maxPerHost * 2 // batasi tekanan ke provider
	return &http.Client{Timeout: timeout, Transport: t}
}
```

- Selalu `http.NewRequestWithContext(ctx, ...)`.
- Selalu `defer resp.Body.Close()` dan baca body sampai habis (`io.Copy(io.Discard, resp.Body)` untuk respons yang tidak dipakai), supaya koneksi kembali ke pool.
- Batasi ukuran respons: `io.LimitReader(resp.Body, maxBytes)`.
- Retry hanya untuk operasi idempoten, dan sebaiknya lewat `jobs` (backoff otomatis), bukan loop retry di dalam request.

## 10. Observability

- `log/slog` JSON. `middleware.RequestLog` membuat request ID, mencatat method, path, status, `duration_ms`, dan IP klien. Logger dibuat dengan `middleware.LogHandler(...)` supaya setiap `log.InfoContext(ctx, ...)` otomatis membawa `request_id`. Jangan pakai `fmt.Println`.
- **Statistik pool** dicatat periodik (mis. tiap 30 detik) atau diekspos sebagai metrik: `db.Stats()` (`InUse`, `Idle`, `WaitCount`, `WaitDuration`). `WaitCount` yang naik terus = pool kurang atau ada transaksi/query yang tertahan.
- **Antrean job**: worker mencatat `pending` dan `oldest_seconds` (umur job tertua yang siap jalan) tiap menit. `oldest_seconds` yang terus naik = worker kurang atau handler lambat. Jumlah job `failed` baru harus memicu alert.
- `net/http/pprof` di **listener internal terpisah** (mis. `127.0.0.1:6060`), tidak di mux publik. Gunakan untuk memeriksa goroutine leak (`/debug/pprof/goroutine`) dan hotspot CPU.
- Diagnosis cepat di MariaDB: `SHOW PROCESSLIST;`, `INNODB_TRX`, slow query log.
- Pasang dari awal. Tanpa ini, bottleneck baru ketahuan setelah user komplain.

## 11. Graceful Shutdown

Urutan di `cmd/server/main.go`, dengan `signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)`:

1. Tandai tidak siap (`/readyz` → 503, §12.5), lalu tunggu `DrainDelay` (≥ interval health check load balancer, mis. 5 detik). Tanpa jeda ini, LB masih mengirim request ke instance yang sudah menolak koneksi baru.
2. `srv.Shutdown(ctx20s)`: berhenti menerima request dan menunggu request yang sedang jalan.
3. `runner.Wait()`: tunggu goroutine background.
4. `db.Close()`, ditutup **terakhir**.

Worker: batalkan ctx `Worker.Run`. Worker berhenti mengambil job baru dan menunggu job yang sedang jalan (maksimal `JobTimeout`, jadi grace period deployment harus lebih besar, §4.4), lalu `db.Close()`. Job yang terputus paksa akan di-reclaim setelah `LockFor`. Kode lengkap ada di [references/struktur-project.md §5](references/struktur-project.md).

## 12. Deploy & Operasional

### 12.1 Migrasi

- `goose up` dijalankan sebagai **langkah terpisah sebelum** binary baru di-roll out (job CI/CD atau init container yang jalan sekali), dengan user DB khusus migrasi (§12.2). Jangan dijalankan saat startup setiap instance, karena beberapa instance akan menjalankan migrasi yang sama bersamaan.
- Selama rolling deploy, binary lama dan baru berjalan bersamaan di atas schema yang sama. Karena itu setiap migrasi harus **kompatibel dengan binary versi sebelumnya** (expand → contract):
  1. Rilis N: tambah kolom/tabel baru (kolom `NULL` atau ber-`DEFAULT`); kode menulis ke struktur lama dan baru.
  2. Isi data lama lewat job (`jobs`, per 1000 ID, §6.4), bukan satu `UPDATE` raksasa di dalam migrasi.
  3. Rilis N+1: kode hanya memakai struktur baru.
  4. Rilis N+2: hapus kolom lama.

  Rename atau drop kolom dalam satu rilis membuat instance lama error selama deploy berlangsung.
- `ALTER TABLE` harus mendapat *metadata lock* dulu. Kalau ada transaksi panjang yang masih terbuka, ALTER menunggu, dan **semua query berikutnya ke tabel itu ikut antre** di belakangnya: aplikasi seolah membeku. DSN migrasi diberi `lock_wait_timeout=5` supaya migrasi gagal cepat lalu bisa diulang. Jangan pakai DSN aplikasi, karena `max_statement_time=10` akan memutus ALTER yang besar.
- Untuk tabel besar, uji migrasinya di salinan data produksi dan catat durasinya sebelum deploy.

### 12.2 User database & hak akses

| User | Hak | Dipakai oleh |
|---|---|---|
| `app` | `SELECT, INSERT, UPDATE, DELETE` per tabel; **hanya `SELECT, INSERT`** di `audit_log`; tanpa DDL | server & worker |
| `app_migrate` | DDL (`CREATE, ALTER, DROP, INDEX`) + DML | langkah migrasi |
| `app_backup` | sesuai kebutuhan alat backup | backup |

Hak di level database (`GRANT ... ON app.*`) tidak bisa dikurangi untuk satu tabel. Karena itu `app` diberi hak per tabel, dan skrip ini dijalankan ulang (oleh `app_migrate` atau DBA) setiap kali migrasi menambah tabel:

```sql
-- hasilnya dijalankan sebagai skrip GRANT
SELECT CONCAT('GRANT SELECT, INSERT, UPDATE, DELETE ON `app`.`', table_name, '` TO ''app''@''10.0.%'';')
FROM information_schema.tables
WHERE table_schema = 'app' AND table_name NOT IN ('audit_log', 'goose_db_version');
-- ditambah satu baris tetap:
GRANT SELECT, INSERT ON `app`.`audit_log` TO 'app'@'10.0.%';
```

Sudah diuji di MariaDB 10.11: user `app` bisa `INSERT` ke `audit_log`, tapi `UPDATE`/`DELETE` di `audit_log` dan `ALTER` di tabel mana pun ditolak (error 1142).

### 12.3 Backup & restore

- Backup fisik harian dengan `mariadb-backup`, ditambah binary log (`log_bin`, `binlog_format=ROW`) untuk point-in-time recovery. `mariadb-dump` hanya cocok untuk database kecil.
- Simpan di lokasi terpisah dari server DB, dalam keadaan terenkripsi (isinya data pasien).
- **Uji restore secara terjadwal** (mis. bulanan) ke server terpisah dan catat durasinya. Backup yang belum pernah di-restore belum terbukti bisa dipakai.
- `SECRET_KEY` (§8.7) disimpan dan dibackup terpisah dari backup DB. Tanpa key itu, kredensial terenkripsi di DB tidak bisa dibaca; sebaliknya, key yang ikut di dalam backup DB membuat enkripsinya percuma.

### 12.4 Reverse proxy & TLS

TLS diterminasi di reverse proxy, sedangkan binary Go hanya mendengarkan di jaringan privat.

```nginx
location / {
    proxy_pass http://app_backend;
    proxy_set_header Host $host;                                  # CrossOriginProtection (§8.5)
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;  # IP klien (§8.6); IP proxy ini masuk TRUSTED_PROXIES
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 10m;   # >= batas MaxBytesReader terbesar (§4.3)
    proxy_read_timeout 35s;     # > WriteTimeout (§4.1)
}
```

- Di produksi: `COOKIE_SECURE=true` (otomatis mengaktifkan HSTS dan cookie `__Host-`), dan HTTP diarahkan ke HTTPS di proxy.
- Port aplikasi dan listener pprof (§10) tidak boleh terbuka ke luar.

### 12.5 Health check

```go
// cmd/server/main.go
var ready atomic.Bool
ready.Store(true)
mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusNoContent) // liveness: proses hidup, TANPA cek DB
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
```

- **Liveness tanpa cek DB.** Kalau DB mati lalu liveness ikut gagal, orchestrator me-restart semua instance berulang kali, padahal restart tidak memperbaiki DB.
- **Readiness** dipakai load balancer: 503 saat DB tidak terjangkau atau saat shutdown (§11), sehingga traffic dialihkan ke instance lain.
- Keduanya publik (tanpa `Require`) dan tidak menampilkan detail error.
- Worker tidak punya endpoint HTTP. Kesehatannya dipantau lewat `oldest_seconds` dan jumlah job `failed` (§10).

## 13. Checklist Review

**Bottleneck & resource**
- [ ] `http.Server` punya `ReadHeaderTimeout`, `ReadTimeout`, `WriteTimeout`, `IdleTimeout`
- [ ] Middleware timeout memberi deadline ke `r.Context()`; semua I/O memakai `ctx` itu (tidak ada `context.Background()` di jalur request)
- [ ] Urutan timeout §4.4 terpenuhi (`RequestTimeout < WriteTimeout`, `JobTimeout < LockFor < grace period`) dan divalidasi `config.Load`
- [ ] `MaxOpenConns` dari config dan dihitung dengan rumus §6.1 (server + worker + cadangan); `ConnMaxLifetime` < `wait_timeout`
- [ ] Tidak ada HTTP call, email, atau sleep di dalam `InTx`; tidak ada pemakaian `s.db`/`s.q` di dalam `InTx`; waktu expiry/`run_at`/lock dihitung dengan `NOW()` di SQL
- [ ] Tidak ada query di dalam loop (N+1); query baru sudah dicek `EXPLAIN`
- [ ] Tidak ada `go func()` mentah di handler/service/task; semua lewat `background.Runner` (goroutine bootstrap di `main.go` dikecualikan)
- [ ] `jobs.Enqueue` dipanggil di dalam `InTx` yang sama dengan data bisnisnya; handler job idempoten dan menghormati `ctx`
- [ ] Worker di proses terpisah dengan `MaxOpen ≥ Concurrency + 2`; `pending`/`oldest_seconds`/`failed` dipantau
- [ ] `UPDATE`/`DELETE` massal di tabel ramai memakai pola "SELECT id → ubah by PK", bertahap (`LIMIT` + loop); deadlock 1213/1205 di-retry dengan mengulang seluruh transaksi
- [ ] Tidak ada sintaks khusus MariaDB yang tidak dikenali sqlc di `queries/*.sql`; batas waktu query lewat `max_statement_time` di DSN
- [ ] `*http.Client` per integrasi dengan `Timeout` & `MaxIdleConnsPerHost`; `resp.Body` selalu ditutup
- [ ] `http.MaxBytesReader` dipasang sebelum parse body
- [ ] Polling HTMX berhenti (286) di semua kondisi akhir, termasuk not found
- [ ] Data tabel FEDERATED yang sering dibaca sudah disinkronkan ke tabel lokal

**Correctness**
- [ ] Template di-parse sekali saat startup, render ke buffer, header di-set sebelum `WriteHeader`
- [ ] Endpoint page & fragment memakai `isFragment(w, r)`: hx-boost & history restore dapat page penuh, header `Vary` terpasang
- [ ] Nama event `HX-Trigger` kebab-case
- [ ] `defer tx.Rollback()` langsung setelah `BeginTx` (otomatis lewat `InTx`)
- [ ] Error tidak diabaikan (`_`), kecuali `json.Marshal` pada map literal yang pasti berhasil
- [ ] `database.Open` dengan `APP_TZ` (Go `Loc` dan `time_zone` sesi sama); tidak ada `loc=Local` di DSN; `main.go` meng-import `time/tzdata`

**Arsitektur & keamanan**
- [ ] Tidak ada state di memory proses (stateless); cache in-process hanya untuk data referensi read-only ber-TTL (§6.6)
- [ ] Tidak ada Redis/broker tambahan tanpa sinyal dari §14 dan catatan di `docs/decisions/`
- [ ] Tidak ada import cycle / `repository.go` pass-through / interface tanpa implementasi kedua
- [ ] `http.CrossOriginProtection` aktif dan proxy meneruskan `Host`; session ID dirotasi saat login; HTMX unauth memakai `HX-Redirect`
- [ ] Token session & client proxy disimpan sebagai hash; session = 1 lookup PK tanpa UPDATE per request; cookie session tanpa `MaxAge`; kredensial BPJS terenkripsi di DB, bukan di `.env`
- [ ] Password bcrypt lewat `auth.Hasher` (paralel dibatasi), panjang 15–72 byte; username tak dikenal mendapat pesan & waktu respons yang sama
- [ ] Batas percobaan login per username & per IP aktif; IP klien hanya dari `middleware.ClientIP` (`X-Forwarded-For` hanya dari `TRUSTED_PROXIES`)
- [ ] Setiap route ber-login memakai `a.Perm(...)` atau `a.Require` dengan sengaja; data dibatasi di query sesuai user/unit, bukan dari parameter request
- [ ] Mutasi & akses baca data sensitif diaudit sinkron (`audit.Write`; di `InTx` untuk mutasi); `detail` tanpa isi data medis/password
- [ ] Ganti password / nonaktifkan user → `DeleteUserSessions`
- [ ] `SecurityHeaders` aktif; tidak ada script/style inline, `hx-on`, `js:`, atau Alpine build non-CSP
- [ ] Logging slog + request ID (`middleware.LogHandler`), statistik pool tercatat, pprof hanya di listener internal
- [ ] Struktur folder sesuai [references/struktur-project.md](references/struktur-project.md)

**Deploy & operasional**
- [ ] Migrasi = langkah terpisah sebelum rollout, kompatibel dengan binary sebelumnya (expand → contract), DSN migrasi memakai `lock_wait_timeout`
- [ ] User DB aplikasi tanpa DDL; `audit_log` hanya `SELECT, INSERT`
- [ ] Backup harian + binlog, restore diuji berkala; `SECRET_KEY` dibackup terpisah dari DB
- [ ] `/healthz` tanpa DB, `/readyz` dengan DB, drain sebelum `Shutdown`; `COOKIE_SECURE=true` + HSTS di produksi; proxy meneruskan `Host` & `X-Forwarded-For`

## 14. Kapan Menambah Redis

Redis (atau broker lain) baru ditambahkan kalau **metrik** menunjukkan salah satu sinyal ini, dan keputusannya dicatat di `docs/decisions/`:

| Sinyal | Yang dipindah | Yang tetap |
|---|---|---|
| Lookup session menyumbang beban berarti ke MariaDB (ribuan request/detik, `WaitCount` naik meski query lain sudah sehat) | session → Redis | aturan §8 (hash, rotasi, `HX-Redirect`) |
| `oldest_seconds` antrean terus naik walau worker & `Concurrency` sudah ditambah, atau volume puluhan ribu job/menit | eksekusi job → broker (mis. asynq) | tabel `jobs` tetap sebagai **outbox**: enqueue tetap di `InTx`, relay memindahkan ke broker |
| Butuh pub/sub lintas instance (notifikasi SSE real-time ke banyak server) atau rate limit global | fitur itu saja | sisanya tetap di MariaDB |
| Query sudah optimal tapi data panas tetap membebani DB, dan cache in-process (§6.6) tidak cukup karena harus konsisten antar instance | cache bersama | invalidasi tetap dipicu dari tulis ke DB |

Yang **bukan** alasan: "biar cepat", "biasanya pakai Redis", atau "nanti traffic besar". Setiap komponen tambahan harus di-deploy, dipantau, dan di-backup, dan menjadi titik gagal baru.

---
*Pedoman, bukan aturan kaku. Sesuaikan dengan kebutuhan nyata dan hindari over-engineering.*
