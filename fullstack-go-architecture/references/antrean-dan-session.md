# Kode Platform: Antrean Job, Background Runner & Session

Implementasi `internal/jobs`, `internal/background`, dan `internal/session` (+ middleware `Require`/`Perm`) yang menggantikan Redis. Login, password, otorisasi, dan audit ada di [auth-dan-keamanan.md](auth-dan-keamanan.md). **Aturan** pemakaiannya ada di `SKILL.md` §7 dan §8, sedangkan **lokasi file**-nya ada di `struktur-project.md`. Dokumen ini hanya berisi kodenya.

Syarat: **MariaDB ≥ 10.6** (`SKIP LOCKED`). Path import memakai module `app`; sesuaikan dengan `go.mod`. Status verifikasi ada di bagian terakhir.

## 1. Migrasi

```sql
-- migrations/20261010000001_jobs_sessions.sql
-- +goose Up
CREATE TABLE jobs (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  kind         VARCHAR(64)  NOT NULL,
  payload      JSON         NOT NULL,
  owner_id     BIGINT       NULL,  -- user yang memicu job; NULL untuk job sistem/periodik
  status       VARCHAR(16)  NOT NULL DEFAULT 'pending', -- pending | running | done | failed
  attempts     INT          NOT NULL DEFAULT 0,
  run_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  locked_until DATETIME     NULL,
  unique_key   VARCHAR(128) NULL,
  last_error   TEXT         NULL,
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_jobs_unique_key (unique_key),
  KEY idx_jobs_status_run_at (status, run_at),
  KEY idx_jobs_status_updated (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE sessions (
  id_hash      BINARY(32)   PRIMARY KEY, -- SHA-256 token cookie; token asli tidak disimpan
  user_id      BIGINT       NOT NULL,
  expires_at   DATETIME     NOT NULL,
  last_seen_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_sessions_user (user_id),
  KEY idx_sessions_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- +goose Down
DROP TABLE sessions;
DROP TABLE jobs;
```

- `(status, run_at)` dipakai query pengambilan job (`EXPLAIN`: `range`, `Using index`).
- `(status, updated_at)` dipakai `ListOldJobIDs`, supaya pencarian job lama tidak memindai semua baris `done`.
- Semua `UPDATE`/`DELETE` massal (`ReclaimJobs`, `DeleteJobs`) lewat PK setelah `SELECT id` biasa; alasannya di SKILL §6.4.
- `unique_key` boleh `NULL` berkali-kali; hanya job periodik yang mengisinya.
- `owner_id` diisi `jobs.EnqueueFor` untuk job yang statusnya dipolling user. `GetJobStatus` memfilter `owner_id`, jadi user lain hanya melihat `not_found`. Tidak perlu index tambahan karena lookup-nya lewat PK.
- `sessions.user_id` di-join ke `users` (migrasi di [auth-dan-keamanan §1](auth-dan-keamanan.md)) supaya user nonaktif langsung kehilangan session-nya.
- Project yang **sudah** men-deploy migrasi versi lama (tanpa `owner_id`): jangan edit file itu. Tambahkan migrasi baru `ALTER TABLE jobs ADD COLUMN owner_id BIGINT NULL AFTER payload;` (SKILL §12.1).

## 2. Query sqlc

```sql
-- queries/jobs.sql
-- name: EnqueueJob :execlastid
INSERT INTO jobs (kind, payload, owner_id) VALUES (?, ?, ?);

-- name: EnqueueUniqueJob :execrows
INSERT IGNORE INTO jobs (kind, payload, unique_key) VALUES (?, ?, ?);

-- name: PickJobs :many
SELECT id, kind, payload, attempts FROM jobs
WHERE status = 'pending' AND run_at <= NOW()
ORDER BY run_at
LIMIT ?
FOR UPDATE SKIP LOCKED;

-- name: MarkJobsRunning :exec
UPDATE jobs
SET status = 'running', attempts = attempts + 1,
    locked_until = DATE_ADD(NOW(), INTERVAL CAST(sqlc.arg(lock_seconds) AS SIGNED) SECOND)
WHERE id IN (sqlc.slice('ids'));

-- name: MarkJobDone :execrows
-- attempts = token klaim: setiap klaim menaikkan attempts. Kalau job sudah diklaim ulang
-- worker lain, hasil worker yang terlambat mendapat 0 baris dan tidak menimpa status job
-- yang sedang jalan. Kalau job baru di-reclaim (belum diklaim ulang), hasilnya tetap
-- tersimpan, jadi job yang sudah sukses tidak dijalankan lagi.
UPDATE jobs SET status = 'done', locked_until = NULL, last_error = NULL
WHERE id = ? AND attempts = ?;

-- name: MarkJobFailed :execrows
UPDATE jobs SET status = 'failed', locked_until = NULL, last_error = ?
WHERE id = ? AND attempts = ?;

-- name: RetryJob :execrows
UPDATE jobs
SET status = 'pending', locked_until = NULL, last_error = sqlc.arg(last_error),
    run_at = DATE_ADD(NOW(), INTERVAL CAST(sqlc.arg(delay_seconds) AS SIGNED) SECOND)
WHERE id = sqlc.arg(id) AND attempts = sqlc.arg(attempts);

-- name: ListStaleJobIDs :many
SELECT id FROM jobs WHERE status = 'running' AND locked_until < NOW() LIMIT 1000;

-- name: ReclaimJobs :execrows
UPDATE jobs
SET status       = IF(attempts >= sqlc.arg(max_attempts), 'failed', 'pending'),
    last_error   = IF(attempts >= sqlc.arg(max_attempts), 'reclaim: melewati LockFor, attempt habis', last_error),
    locked_until = NULL
WHERE id IN (sqlc.slice('ids')) AND status = 'running' AND locked_until < NOW();

-- name: ListOldJobIDs :many
SELECT id FROM jobs
WHERE status IN ('done', 'failed') AND updated_at < DATE_SUB(NOW(), INTERVAL CAST(sqlc.arg(keep_days) AS SIGNED) DAY)
LIMIT 1000;

-- name: DeleteJobs :execrows
DELETE FROM jobs WHERE id IN (sqlc.slice('ids')) AND status IN ('done', 'failed');

-- name: GetJobStatus :one
SELECT status FROM jobs WHERE id = ? AND owner_id = ?;

-- name: CountPendingJobs :one
SELECT COUNT(*) AS pending,
       CAST(COALESCE(TIMESTAMPDIFF(SECOND, MIN(run_at), NOW()), 0) AS SIGNED) AS oldest_seconds
FROM jobs WHERE status = 'pending' AND run_at <= NOW();
```

```sql
-- queries/sessions.sql
-- name: CreateSession :exec
INSERT INTO sessions (id_hash, user_id, expires_at)
VALUES (?, ?, DATE_ADD(NOW(), INTERVAL CAST(sqlc.arg(ttl_seconds) AS SIGNED) SECOND));

-- name: GetSession :one
SELECT s.user_id, u.role,
       CAST(TIMESTAMPDIFF(SECOND, s.last_seen_at, NOW()) AS SIGNED) AS idle_seconds
FROM sessions s
JOIN users u ON u.id = s.user_id
WHERE s.id_hash = ? AND s.expires_at > NOW() AND u.is_active = TRUE;

-- name: TouchSession :exec
-- Perpanjangan idle tidak pernah melewati batas mutlak created_at + max_age:
-- user yang terus aktif tetap wajib login ulang (mis. tiap 12 jam).
-- expires_at > NOW(): session yang sudah kedaluwarsa tidak pernah dihidupkan lagi
-- (DeleteExpiredSessions bergantung pada ini).
UPDATE sessions
SET last_seen_at = NOW(),
    expires_at = LEAST(
      DATE_ADD(NOW(), INTERVAL CAST(sqlc.arg(ttl_seconds) AS SIGNED) SECOND),
      DATE_ADD(created_at, INTERVAL CAST(sqlc.arg(max_age_seconds) AS SIGNED) SECOND))
WHERE id_hash = sqlc.arg(id_hash) AND expires_at > NOW();

-- name: DeleteSession :exec
DELETE FROM sessions WHERE id_hash = ?;

-- name: DeleteUserSessions :exec
DELETE FROM sessions WHERE user_id = ?;

-- name: ListExpiredSessionIDs :many
-- Cleanup lewat PK (SKILL §6.4): DELETE by range di idx_sessions_expires mengunci juga
-- session AKTIF pertama sesudah range, sehingga TouchSession user itu menunggu/deadlock.
SELECT id_hash FROM sessions WHERE expires_at < NOW() LIMIT 1000;

-- name: DeleteExpiredSessions :execrows
-- Sengaja TANPA "AND expires_at < NOW()": dengan kondisi itu optimizer kadang memilih
-- idx_sessions_expires (range) lagi, bukan PK (terlihat di EXPLAIN). Aman, karena session
-- kedaluwarsa tidak pernah diperpanjang (TouchSession mensyaratkan expires_at > NOW()).
DELETE FROM sessions WHERE id_hash IN (sqlc.slice('ids'));
```

Semua waktu dihitung dengan `NOW()` milik DB (SKILL §6.3); pola `CAST(sqlc.arg(x) AS SIGNED)` dijelaskan di struktur-project §6.

## 3. `internal/jobs`

```go
// internal/jobs/jobs.go
package jobs

import (
	"context"
	"database/sql"
	"encoding/json"

	"app/internal/store"
)

// Daftar kind & payload. Package ini tidak boleh mengimpor domain.
const (
	KindOrderSync           = "order:sync"
	KindSessionCleanup      = "session:cleanup"
	KindLoginFailureCleanup = "login:cleanup"
	KindJobCleanup          = "job:cleanup" // didaftarkan otomatis oleh NewWorker
)

type OrderSync struct {
	OrderID int64 `json:"order_id"`
}

// Enqueue menulis job lewat q dari InTx, jadi job ikut commit atau rollback
// bersama data bisnisnya. Job tanpa pemilik tidak bisa dipolling lewat GET /jobs/{id}.
func Enqueue(ctx context.Context, q *store.Queries, kind string, payload any) (int64, error) {
	return enqueue(ctx, q, sql.NullInt64{}, kind, payload)
}

// EnqueueFor = Enqueue untuk job yang statusnya dipolling user (SKILL §7.3).
// Hanya ownerID yang bisa membaca statusnya.
func EnqueueFor(ctx context.Context, q *store.Queries, ownerID int64, kind string, payload any) (int64, error) {
	return enqueue(ctx, q, sql.NullInt64{Int64: ownerID, Valid: true}, kind, payload)
}

func enqueue(ctx context.Context, q *store.Queries, owner sql.NullInt64, kind string, payload any) (int64, error) {
	b, err := json.Marshal(payload)
	if err != nil {
		return 0, err
	}
	return q.EnqueueJob(ctx, store.EnqueueJobParams{Kind: kind, Payload: b, OwnerID: owner})
}
```

```go
// internal/jobs/worker.go
package jobs

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"runtime/debug"
	"sync"
	"time"

	"app/internal/database"
	"app/internal/store"
)

// Handler menjalankan satu job dan wajib idempoten: job bisa jalan lebih dari sekali
// (retry, atau proses mati setelah handler sukses tapi sebelum status tersimpan).
type Handler func(ctx context.Context, payload json.RawMessage) error

// ErrPermanent: bungkus dengan fmt.Errorf("...: %w", jobs.ErrPermanent) supaya job
// langsung 'failed' tanpa retry (payload rusak, data sudah tidak valid).
var ErrPermanent = errors.New("permanent")

// Batasan antar-nilai (pool, JobTimeout < LockFor): SKILL §4.4 dan §7.2.
type Config struct {
	Concurrency  int           // job paralel per proses
	PollInterval time.Duration // jeda saat antrean kosong, mis. 1s
	JobTimeout   time.Duration // batas satu job
	LockFor      time.Duration // job 'running' melewati ini dianggap prosesnya mati
	MaxAttempts  int
	KeepDays     int // retensi job done/failed
}

type Worker struct {
	db       *sql.DB
	q        *store.Queries
	log      *slog.Logger
	cfg      Config
	handlers map[string]Handler
	periodic map[string]time.Duration
}

func NewWorker(db *sql.DB, log *slog.Logger, cfg Config) *Worker {
	w := &Worker{db: db, q: store.New(db), log: log, cfg: cfg,
		handlers: map[string]Handler{}, periodic: map[string]time.Duration{}}
	// Hapus job lama sebagai job periodik: hanya satu instance yang menjalankannya per slot,
	// bukan setiap instance setiap menit.
	w.Handle(KindJobCleanup, w.cleanup)
	w.Every(KindJobCleanup, 10*time.Minute)
	return w
}

func (w *Worker) Handle(kind string, h Handler) { w.handlers[kind] = h }

// Every menjadwalkan kind secara periodik (resolusi 1 menit). Aman multi-instance:
// unique_key per slot waktu membuat hanya satu INSERT yang berhasil.
func (w *Worker) Every(kind string, every time.Duration) { w.periodic[kind] = every }

// Run memblokir sampai ctx dibatalkan, lalu menunggu job yang sedang jalan selesai.
func (w *Worker) Run(ctx context.Context) error {
	sem := make(chan struct{}, w.cfg.Concurrency)
	// done membangunkan loop begitu ada slot kosong. Tanpa ini, slot yang kosong menunggu
	// tick poll berikutnya, dan throughput mentok di Concurrency/PollInterval untuk job pendek.
	done := make(chan struct{}, 1)
	var wg sync.WaitGroup
	defer wg.Wait()

	poll := time.NewTicker(w.cfg.PollInterval)
	defer poll.Stop()
	maint := time.NewTicker(time.Minute)
	defer maint.Stop()
	w.maintain(ctx)

	for {
		if free := cap(sem) - len(sem); free > 0 {
			batch, err := w.claim(ctx, free)
			switch {
			case err == nil || ctx.Err() != nil:
			case database.IsRetryable(err): // bentrok dengan worker lain; tx sudah rollback, job tetap pending
				w.log.Warn("ambil job bentrok, diulang", "err", err)
			default:
				w.log.Error("ambil job gagal", "err", err)
			}
			for _, j := range batch {
				sem <- struct{}{}
				wg.Go(func() {
					defer func() {
						<-sem
						select {
						case done <- struct{}{}:
						default: // sinyal sudah menunggu, cukup satu
						}
					}()
					w.run(j)
				})
			}
			if len(batch) == free {
				continue // antrean masih ada: ambil lagi tanpa menunggu poll
			}
		}
		select {
		case <-ctx.Done():
			return nil
		case <-maint.C:
			w.maintain(ctx)
		case <-done:
		case <-poll.C:
		}
	}
}

// claim: transaksi pendek (SELECT ... SKIP LOCKED + UPDATE), handler jalan DI LUAR transaksi.
func (w *Worker) claim(ctx context.Context, n int) ([]store.PickJobsRow, error) {
	var batch []store.PickJobsRow
	err := database.InTx(ctx, w.db, func(q *store.Queries) error {
		var err error
		if batch, err = q.PickJobs(ctx, int32(n)); err != nil || len(batch) == 0 {
			return err
		}
		ids := make([]int64, len(batch))
		for i, j := range batch {
			ids[i] = j.ID
		}
		return q.MarkJobsRunning(ctx, store.MarkJobsRunningParams{
			LockSeconds: int64(w.cfg.LockFor / time.Second), Ids: ids})
	})
	if err != nil {
		return nil, err
	}
	return batch, nil
}

func (w *Worker) run(j store.PickJobsRow) {
	attempt := int(j.Attempts) + 1
	log := w.log.With("job_id", j.ID, "kind", j.Kind, "attempt", attempt)

	ctx, cancel := context.WithTimeout(context.Background(), w.cfg.JobTimeout)
	err := w.call(ctx, j)
	cancel()

	// context baru: ctx job bisa sudah habis, tapi hasilnya tetap harus tercatat
	ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	// Semua query status memakai id + attempts klaim ini: kalau job sudah diklaim ulang
	// worker lain, update-nya 0 baris dan tidak menimpa status job yang sedang jalan.
	claimed := int32(attempt)
	var save func() (int64, error)
	switch {
	case err == nil:
		p := store.MarkJobDoneParams{ID: j.ID, Attempts: claimed}
		save = func() (int64, error) { return w.q.MarkJobDone(ctx, p) }
	case errors.Is(err, ErrPermanent) || attempt >= w.cfg.MaxAttempts:
		log.Error("job gagal permanen", "err", err)
		p := store.MarkJobFailedParams{ID: j.ID, Attempts: claimed,
			LastError: sql.NullString{String: err.Error(), Valid: true}}
		save = func() (int64, error) { return w.q.MarkJobFailed(ctx, p) }
	default:
		log.Warn("job gagal, dijadwalkan ulang", "err", err)
		p := store.RetryJobParams{ID: j.ID, Attempts: claimed,
			DelaySeconds: 10 << min(attempt, 10), // 20s, 40s, ... maks ~3 jam
			LastError:    sql.NullString{String: err.Error(), Valid: true}}
		save = func() (int64, error) { return w.q.RetryJob(ctx, p) }
	}
	// Status gagal tersimpan = job di-reclaim setelah LockFor dan jalan dua kali.
	// Deadlock/lock wait boleh di-retry (SKILL §6.4); update status idempoten.
	var n int64
	for try := 1; ; try++ {
		n, err = save()
		if err == nil || try == 3 || !database.IsRetryable(err) {
			break
		}
		log.Warn("simpan status job di-retry", "try", try, "err", err)
	}
	switch {
	case err != nil:
		log.Error("status job gagal disimpan, akan di-reclaim setelah LockFor", "err", err)
	case n == 0:
		// handler berjalan melewati LockFor (mengabaikan ctx?): job sudah diklaim ulang
		log.Warn("status job tidak disimpan: job sudah diklaim ulang worker lain")
	}
}

func (w *Worker) call(ctx context.Context, j store.PickJobsRow) (err error) {
	h, ok := w.handlers[j.Kind]
	if !ok {
		return fmt.Errorf("kind %q tidak terdaftar: %w", j.Kind, ErrPermanent)
	}
	defer func() {
		if v := recover(); v != nil {
			err = fmt.Errorf("panic: %v\n%s", v, debug.Stack())
		}
	}()
	return h(ctx, j.Payload)
}

// maintain (tiap menit, di semua instance, idempoten): reclaim job macet,
// jadwalkan job periodik, catat panjang antrean. Semuanya query ringan ber-index.
func (w *Worker) maintain(ctx context.Context) {
	if n, err := w.reclaim(ctx); err != nil && ctx.Err() == nil {
		w.log.Error("reclaim job gagal", "err", err)
	} else if n > 0 {
		w.log.Warn("job macet dikembalikan ke antrean", "count", n)
	}
	// Slot dihitung dari jam proses, jadi jam semua instance worker wajib sinkron (NTP).
	// Selisih jam di batas slot bisa menjadwalkan satu job periodik dua kali; karena itu
	// handler periodik juga harus idempoten (semua cleanup di sini idempoten).
	now := time.Now()
	for kind, every := range w.periodic {
		key := fmt.Sprintf("%s:%d", kind, now.Truncate(every).Unix())
		if _, err := w.q.EnqueueUniqueJob(ctx, store.EnqueueUniqueJobParams{Kind: kind,
			Payload: json.RawMessage("{}"), UniqueKey: sql.NullString{String: key, Valid: true}}); err != nil {
			w.log.Error("jadwal job periodik gagal", "kind", kind, "err", err)
		}
	}
	if st, err := w.q.CountPendingJobs(ctx); err == nil {
		w.log.Info("antrean job", "pending", st.Pending, "oldest_seconds", st.OldestSeconds)
	}
}

// reclaim: cari ID dengan SELECT biasa (tanpa lock), lalu UPDATE lewat PK dengan kondisi diulang.
func (w *Worker) reclaim(ctx context.Context) (int64, error) {
	ids, err := w.q.ListStaleJobIDs(ctx)
	if err != nil || len(ids) == 0 {
		return 0, err
	}
	return w.q.ReclaimJobs(ctx, store.ReclaimJobsParams{MaxAttempts: int32(w.cfg.MaxAttempts), Ids: ids})
}

// cleanup = handler KindJobCleanup. Hapus per 1000 baris lewat PK sampai habis.
func (w *Worker) cleanup(ctx context.Context, _ json.RawMessage) error {
	for {
		ids, err := w.q.ListOldJobIDs(ctx, int64(w.cfg.KeepDays))
		if err != nil || len(ids) == 0 {
			return err
		}
		if _, err := w.q.DeleteJobs(ctx, ids); err != nil {
			return err
		}
		if len(ids) < 1000 {
			return nil
		}
	}
}
```

```go
// internal/jobs/status.go — polling status dari HTMX (aturan: SKILL §7.3).
// Namanya StatusHandler karena jobs.Handler sudah dipakai untuk handler job.
package jobs

import (
	"database/sql"
	"errors"
	"log/slog"
	"net/http"
	"strconv"

	"app/internal/middleware"
	"app/internal/render"
	"app/internal/session"
	"app/internal/store"
)

type StatusHandler struct {
	q    *store.Queries
	view *render.Engine
	log  *slog.Logger
}

func NewStatusHandler(q *store.Queries, view *render.Engine, log *slog.Logger) *StatusHandler {
	return &StatusHandler{q: q, view: view, log: log}
}

func (h *StatusHandler) Routes(mux *http.ServeMux, a *middleware.Auth) {
	mux.Handle("GET /jobs/{id}", a.Require(http.HandlerFunc(h.Status)))
}

func (h *StatusHandler) Status(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		http.Error(w, "id tidak valid", http.StatusBadRequest)
		return
	}
	user, _ := session.UserFrom(r.Context())
	// owner_id = user di WHERE: job milik orang lain tampil sebagai not_found, tidak membocorkan keberadaannya
	status, err := h.q.GetJobStatus(r.Context(), store.GetJobStatusParams{
		ID: id, OwnerID: sql.NullInt64{Int64: user.ID, Valid: true}})
	code := http.StatusOK
	switch {
	case errors.Is(err, sql.ErrNoRows):
		status, code = "not_found", 286 // 286 = htmx berhenti polling
	case err != nil:
		h.log.ErrorContext(r.Context(), "status job gagal dibaca", "job_id", id, "err", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	case status == "done" || status == "failed":
		code = 286
	}
	h.view.Render(w, r, code, "jobs/_status", map[string]any{"Status": status})
}
```

## 4. Handler job di domain

```go
// internal/orders/tasks.go (potongan)
package orders

import (
	"context"
	"encoding/json"
	"fmt"

	"app/internal/jobs"
)

type Service struct{}

func (s *Service) syncToBPJS(ctx context.Context, orderID int64) error { return nil }

// HandleOrderSync didaftarkan di cmd/worker: w.Handle(jobs.KindOrderSync, svc.HandleOrderSync).
func (s *Service) HandleOrderSync(ctx context.Context, payload json.RawMessage) error {
	var p jobs.OrderSync
	if err := json.Unmarshal(payload, &p); err != nil {
		return fmt.Errorf("payload order sync: %w", jobs.ErrPermanent)
	}
	// idempoten: syncToBPJS cek dulu status sync order di DB, lewati kalau sudah 'synced'
	return s.syncToBPJS(ctx, p.OrderID)
}
```

## 5. `cmd/worker/main.go`

```go
// cmd/worker/main.go
package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	authh "app/internal/auth"
	"app/internal/config"
	"app/internal/database"
	"app/internal/jobs"
	"app/internal/middleware"
	"app/internal/orders"
	"app/internal/session"
	"app/internal/store"
)

func main() {
	if err := run(); err != nil {
		slog.Error("worker berhenti", "err", err)
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
	log := slog.New(middleware.LogHandler(slog.NewJSONHandler(os.Stdout, nil)))

	db, err := database.Open(cfg.DB) // pool milik worker sendiri, ukuran: SKILL §7.2
	if err != nil {
		return err
	}
	defer db.Close()
	q := store.New(db)

	w := jobs.NewWorker(db, log, jobs.Config(cfg.Jobs))

	ordersSvc := &orders.Service{}                          // orders.NewService(db, q, ...) di project nyata
	w.Handle(jobs.KindOrderSync, ordersSvc.HandleOrderSync) // satu baris per kind

	sessions := session.NewStore(q, cfg.SessionTTL, cfg.SessionMaxAge, cfg.CookieSecure)
	w.Handle(jobs.KindSessionCleanup, sessions.HandleCleanup)
	w.Every(jobs.KindSessionCleanup, time.Hour)

	throttle := authh.NewThrottle(db) // hapus login_failures lama
	w.Handle(jobs.KindLoginFailureCleanup, throttle.HandleCleanup)
	w.Every(jobs.KindLoginFailureCleanup, time.Hour)
	// job:cleanup (hapus job lama) sudah didaftarkan oleh NewWorker

	return w.Run(ctx) // blok sampai SIGTERM, lalu tunggu job yang sedang jalan
}
```

`config.Jobs` punya field yang sama dengan `jobs.Config`, sehingga cukup dikonversi langsung. Nilai awal yang wajar: `Concurrency=8`, `PollInterval=1s`, `JobTimeout=60s`, `LockFor=5m`, `MaxAttempts=8`, `KeepDays=7`. Dengan `MaxAttempts=8` ada 7 retry berjeda 20s, 40s, … 1280s, jadi job baru `failed` sekitar **42 menit** setelah percobaan pertama.

## 6. `internal/background`

Goroutine in-process yang terbatas, untuk pekerjaan yang boleh hilang (aturan: SKILL §7.1).

```go
// internal/background/runner.go
package background

import (
	"context"
	"log/slog"
	"runtime/debug"
	"sync"
	"time"
)

type Runner struct {
	sem chan struct{}
	wg  sync.WaitGroup
	log *slog.Logger
}

func New(max int, log *slog.Logger) *Runner {
	return &Runner{sem: make(chan struct{}, max), log: log}
}

// Go menjalankan fn di goroutine; menolak (return false) kalau penuh, bukan memblokir request.
func (r *Runner) Go(ctx context.Context, name string, timeout time.Duration, fn func(context.Context)) bool {
	select {
	case r.sem <- struct{}{}:
	default:
		r.log.WarnContext(ctx, "background penuh, task dilewati", "task", name)
		return false
	}
	ctx = context.WithoutCancel(ctx) // tetap membawa value (request ID), lepas dari cancel request
	r.wg.Go(func() {
		defer func() { <-r.sem }()
		defer func() {
			if v := recover(); v != nil {
				r.log.Error("panic di background task", "task", name, "panic", v, "stack", string(debug.Stack()))
			}
		}()
		ctx, cancel := context.WithTimeout(ctx, timeout)
		defer cancel()
		fn(ctx)
	})
	return true
}

// Wait dipanggil saat shutdown, setelah srv.Shutdown.
func (r *Runner) Wait() { r.wg.Wait() }
```

## 7. `internal/session`

```go
// internal/session/session.go
package session

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"app/internal/store"
)

var ErrNoSession = errors.New("session tidak ada atau kedaluwarsa")

// User = identitas yang ditaruh di context oleh middleware Require.
type User struct {
	ID   int64
	Role string
}

type Store struct {
	q      *store.Queries
	ttl    time.Duration // idle: session habis kalau tidak dipakai selama ini
	maxAge time.Duration // mutlak: session habis sejak login, walau terus dipakai
	secure bool
	cookie string
}

// NewStore: ttl ≤ maxAge, divalidasi config.Load (mis. SESSION_TTL=30m, SESSION_MAX_AGE=12h).
func NewStore(q *store.Queries, ttl, maxAge time.Duration, secure bool) *Store {
	// Prefix __Host- membuat browser menolak cookie ini kalau tanpa Secure, dengan Domain,
	// atau Path selain "/", sehingga subdomain lain tidak bisa menimpanya. Hanya bisa di HTTPS.
	name := "session_id"
	if secure {
		name = "__Host-session_id"
	}
	return &Store{q: q, ttl: ttl, maxAge: maxAge, secure: secure, cookie: name}
}

// hash: DB hanya menyimpan SHA-256 token, jadi bocornya tabel sessions tidak bisa dipakai login.
func hash(token string) []byte {
	h := sha256.Sum256([]byte(token))
	return h[:]
}

// Login dipanggil setelah password valid. Session lama dihapus (rotasi, cegah session fixation).
func (s *Store) Login(ctx context.Context, w http.ResponseWriter, r *http.Request, userID int64) error {
	if c, err := r.Cookie(s.cookie); err == nil {
		if err := s.q.DeleteSession(ctx, hash(c.Value)); err != nil {
			return err
		}
	}
	token := rand.Text() // 128+ bit, crypto/rand
	if err := s.q.CreateSession(ctx, store.CreateSessionParams{
		IDHash: hash(token), UserID: userID, TtlSeconds: int64(s.ttl / time.Second)}); err != nil {
		return err
	}
	// sengaja tanpa MaxAge/Expires (SKILL §8)
	http.SetCookie(w, &http.Cookie{Name: s.cookie, Value: token, Path: "/",
		HttpOnly: true, Secure: s.secure, SameSite: http.SameSiteLaxMode})
	return nil
}

// User membaca session: 1 lookup PK (+ join PK ke users) per request. User nonaktif langsung
// dianggap tidak punya session, dan perubahan role berlaku di request berikutnya.
// Kedaluwarsa diperpanjang paling sering tiap 5 menit, supaya tidak ada UPDATE di setiap request.
func (s *Store) User(r *http.Request) (User, error) {
	c, err := r.Cookie(s.cookie)
	if err != nil {
		return User{}, ErrNoSession
	}
	id := hash(c.Value)
	row, err := s.q.GetSession(r.Context(), id)
	if errors.Is(err, sql.ErrNoRows) {
		return User{}, ErrNoSession
	}
	if err != nil {
		return User{}, err // DB bermasalah: bukan alasan untuk redirect ke login
	}
	if row.IdleSeconds > 300 {
		if err := s.q.TouchSession(r.Context(), store.TouchSessionParams{IDHash: id,
			TtlSeconds: int64(s.ttl / time.Second), MaxAgeSeconds: int64(s.maxAge / time.Second)}); err != nil {
			return User{}, err
		}
	}
	return User{ID: row.UserID, Role: row.Role}, nil
}

func (s *Store) Logout(ctx context.Context, w http.ResponseWriter, r *http.Request) error {
	if c, err := r.Cookie(s.cookie); err == nil {
		if err := s.q.DeleteSession(ctx, hash(c.Value)); err != nil {
			return err
		}
	}
	http.SetCookie(w, &http.Cookie{Name: s.cookie, Value: "", Path: "/", MaxAge: -1,
		HttpOnly: true, Secure: s.secure, SameSite: http.SameSiteLaxMode})
	return nil
}

// HandleCleanup dipasang di worker: w.Every(jobs.KindSessionCleanup, time.Hour).
// Pola SELECT id → DELETE by PK per 1000 (SKILL §6.4), supaya tidak deadlock dengan TouchSession.
func (s *Store) HandleCleanup(ctx context.Context, _ json.RawMessage) error {
	for {
		ids, err := s.q.ListExpiredSessionIDs(ctx)
		if err != nil || len(ids) == 0 {
			return err
		}
		if _, err := s.q.DeleteExpiredSessions(ctx, ids); err != nil {
			return err
		}
		if len(ids) < 1000 {
			return nil
		}
	}
}

type ctxKey struct{}

func WithUser(ctx context.Context, u User) context.Context {
	return context.WithValue(ctx, ctxKey{}, u)
}

func UserFrom(ctx context.Context) (User, bool) {
	u, ok := ctx.Value(ctxKey{}).(User)
	return u, ok
}
```

`Login`/`Logout` dipanggil oleh handler di `internal/auth` (kode lengkap, termasuk batas percobaan dan audit: [auth-dan-keamanan §5](auth-dan-keamanan.md)). Setelah ganti password atau menonaktifkan user, panggil `DeleteUserSessions`.

Middleware `Require` (wajib login) dan `Perm` (wajib login + izin), aturannya di SKILL §8. Keduanya dipasang per route di `Routes(mux, a *middleware.Auth)`:

```go
// internal/middleware/auth.go
package middleware

import (
	"errors"
	"log/slog"
	"net/http"

	"app/internal/authz"
	"app/internal/session"
)

type Auth struct {
	sessions *session.Store
	log      *slog.Logger
}

func NewAuth(sessions *session.Store, log *slog.Logger) *Auth {
	return &Auth{sessions: sessions, log: log}
}

// Require: wajib login. Halaman ber-session tidak boleh disimpan cache browser/proxy
// (tombol Back setelah logout, komputer bersama di loket).
func (m *Auth) Require(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		user, err := m.sessions.User(r) // cookie → hash → 1 lookup PK
		switch {
		case errors.Is(err, session.ErrNoSession):
			if r.Header.Get("HX-Request") == "true" {
				w.Header().Set("HX-Redirect", "/login")
				w.WriteHeader(http.StatusUnauthorized)
				return
			}
			http.Redirect(w, r, "/login", http.StatusSeeOther)
			return
		case err != nil: // DB bermasalah: 500, bukan redirect login
			m.log.ErrorContext(r.Context(), "baca session gagal", "err", err)
			http.Error(w, "internal server error", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Cache-Control", "no-store")
		next.ServeHTTP(w, r.WithContext(session.WithUser(r.Context(), user)))
	})
}

// Perm: wajib login DAN punya izin p. Dipasang per route:
// mux.Handle("POST /patients", a.Perm(authz.PatientWrite)(http.HandlerFunc(h.Create))).
func (m *Auth) Perm(p authz.Perm) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return m.Require(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			user, _ := session.UserFrom(r.Context())
			if !authz.Can(user.Role, p) {
				m.log.WarnContext(r.Context(), "akses ditolak", "user_id", user.ID, "role", user.Role, "perm", p)
				http.Error(w, "akses ditolak", http.StatusForbidden)
				return
			}
			next.ServeHTTP(w, r)
		}))
	}
}
```

Di handler, user diambil dengan `session.UserFrom(r.Context())` (berisi `ID` dan `Role`).

## 8. Operasional

```sql
-- kesehatan antrean
SELECT status, COUNT(*) FROM jobs GROUP BY status;
-- job gagal terbaru
SELECT id, kind, attempts, updated_at, LEFT(last_error, 200) FROM jobs
WHERE status = 'failed' ORDER BY updated_at DESC LIMIT 20;
-- jalankan ulang job failed setelah penyebabnya diperbaiki
UPDATE jobs SET status = 'pending', attempts = 0, run_at = NOW() WHERE id = ?;
-- job yang sedang jalan dan lock-nya
SELECT id, kind, attempts, locked_until FROM jobs WHERE status = 'running';
-- paksa logout semua perangkat seorang user
DELETE FROM sessions WHERE user_id = ?;
```

## 9. Hasil Verifikasi

Kode di dokumen ini (salinan persis dari kode yang diuji) di-generate dengan sqlc v1.30, lolos `go vet` + `gofmt`, dan diuji `go test -race` dengan Go 1.25.3 terhadap MariaDB 10.11.13 (REPEATABLE READ, default).

| Skenario | Hasil |
|---|---|
| `Enqueue` di `InTx` yang di-rollback | job ikut batal; yang commit tersimpan |
| 3 instance worker (`Concurrency=8`, `PollInterval=1s`) × 2000 job @5ms, diulang 20× | ±0,8–0,9 detik per run (desain lama yang hanya mengisi slot tiap poll: ±83 detik); **0 duplikat**, semua `done`, 0 deadlock |
| Stres: 3 worker × 3000 job + cleanup & reclaim berjalan terus-menerus, 20.000 job lama, diulang 10× | semua `done`, 20.000 job lama terhapus, 0 error |
| `UPDATE` status job lain selama transaksi cleanup ditahan terbuka | langsung jalan (±1 ms). Dengan `DELETE ... WHERE range` versi lama: **terblokir** sampai lock wait timeout, baik dengan maupun tanpa index, di RR maupun RC |
| `MarkJobDone` bersamaan dengan `DELETE ... WHERE range` versi lama | **deadlock 1213** di ±40% run → status hilang → job jalan dua kali setelah `LockFor`. Hilang setelah mutasi lewat PK |
| `running` lewat `LockFor`: `attempts ≥ MaxAttempts` / di bawahnya / belum lewat | `failed` (dengan `last_error`) / `pending` / tetap `running` |
| `EXPLAIN` `ListOldJobIDs` | memakai `idx_jobs_status_updated` |
| Cleanup 2.500 job lama + 10 job baru | 2.500 terhapus dalam beberapa batch, 10 tersisa |
| Error sementara / panic | `pending`, `attempts=1`, `run_at` di masa depan (backoff) |
| `ErrPermanent` / kind tidak terdaftar | langsung `failed` dengan `last_error` |
| 5 instance menjadwalkan job periodik yang sama bersamaan | hanya **1** job tercatat |
| Worker A memegang `FOR UPDATE SKIP LOCKED` selama 3 detik | `INSERT` job baru tidak terblokir; worker B langsung mendapat job lain |
| Session: login → baca → idle 10 menit → baca | expiry diperpanjang; tanpa UPDATE kalau idle < 5 menit |
| Login ulang dengan cookie lama / logout / kedaluwarsa | session lama tidak berlaku / terhapus / dibersihkan cleanup |

Lima baris terakhir berasal dari pengujian versi awal.

**Revisi 2026-10-10** (`owner_id`, session ber-role, `Require`/`Perm`, `StatusHandler` per pemilik), diuji ulang dengan Go 1.26.0, `go-sql-driver/mysql` v1.10.1, dan MariaDB 10.11.13 (`go test -race`):

| Skenario | Hasil |
|---|---|
| `EnqueueFor` user A, polling oleh A / user B / job tanpa pemilik | 200 `pending` / 286 `not_found` / 286 `not_found` |
| Job selesai | 286 `done` |
| Worker mengambil job ber-`owner_id` dan tanpa pemilik | keduanya `done` |
| Role diubah atau user dinonaktifkan di DB | berlaku di request berikutnya; user nonaktif → redirect login / 401 + `HX-Redirect` |
| Session setelah logout | tidak berlaku lagi |
| Cookie di HTTPS / HTTP | `__Host-session_id` / `session_id`, tanpa `MaxAge` |
| `Require` | menambah `Cache-Control: no-store` |

**Revisi review 2026-10-10** (status job per klaim, batas mutlak session, cleanup session lewat PK, collation eksplisit). Kode diekstrak langsung dari dokumen ini, `sqlc diff` bersih, lolos `go vet` + `gofmt`, dan diuji `go test -race` dengan Go 1.27.2, sqlc v1.30.0, dan MariaDB 10.11.13:

| Skenario | Hasil |
|---|---|
| Worker A klaim job, job di-reclaim lalu diklaim worker B, kemudian hasil A datang (gagal → retry / sukses) | update A = 0 baris, job tetap `running` milik B (`attempts=2`); hasil B tersimpan (`pending` / `done`) |
| Job di-reclaim (belum diklaim ulang), lalu hasil sukses worker A datang | `done`; job tidak dijalankan lagi |
| `EXPLAIN` `ReclaimJobs` (20.000 `done`, 500 `pending`, 3–200 `running` yang semuanya stale) dan `DeleteJobs` (1.000 ID) | selalu `key=PRIMARY` |
| Session login 11j55m lalu, aktif terus (`TTL=30m`, `MAX_AGE=12h`) | `expires_at` diperpanjang hanya sampai ±5 menit lagi (batas mutlak) |
| Session login 1 jam lalu, idle 10 menit | `expires_at` diperpanjang 30 menit |
| Session lewat batas mutlak | `ErrNoSession` |
| Cleanup 2.500 session kedaluwarsa + 3 aktif | 2.500 terhapus dalam beberapa batch, 3 tersisa |
| Transaksi cleanup ditahan terbuka (5.000 session aktif, 50 kedaluwarsa), lalu `TouchSession` session aktif terdekat | versi lama (`DELETE ... WHERE expires_at < NOW()`): **terblokir 2 detik → 1205**; versi baru (SELECT id → DELETE by PK): ±1 ms |
| `EXPLAIN` DELETE by PK **dengan** kondisi `expires_at < NOW()` diulang | rencana tidak stabil: kadang `PRIMARY`, kadang `idx_sessions_expires` (range). Karena itu `DeleteExpiredSessions` tanpa kondisi tersebut |

Rincian uji login, izin, dan audit ada di [auth-dan-keamanan §10](auth-dan-keamanan.md). `background.Runner` masih hanya diperiksa dengan `go vet`, belum punya test.
