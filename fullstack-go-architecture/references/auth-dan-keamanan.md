# Kode Keamanan: Login, Otorisasi, Audit & Header

Implementasi `internal/auth` (domain login: password, batas percobaan, handler), `internal/authz` (peran → izin), `internal/audit`, serta middleware `RequestLog` (request ID + IP klien) dan `SecurityHeaders`. **Aturan** pemakaiannya ada di `SKILL.md` §5 butir 8 dan §8, **lokasi file** di `struktur-project.md`, sedangkan session & middleware `Require`/`Perm` di `antrean-dan-session.md` §7. Dokumen ini hanya berisi kodenya.

Path import memakai module `app`. Dependensi tambahan satu-satunya: `golang.org/x/crypto` (bcrypt). Status verifikasi ada di bagian terakhir.

## 1. Migrasi

```sql
-- +goose Up
CREATE TABLE users (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  username      VARCHAR(64)  NOT NULL,          -- unicode_ci: unik tanpa beda huruf besar/kecil & aksen
  password_hash VARCHAR(100) NOT NULL,          -- bcrypt (60 karakter)
  role          VARCHAR(32)  NOT NULL,          -- kunci di authz.roles
  is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_users_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE login_failures (
  id         BIGINT AUTO_INCREMENT PRIMARY KEY,
  key_hash   BINARY(32) NOT NULL,               -- SHA-256 "u:<WEIGHT_STRING username>" atau "ip:<alamat|/64>"
  created_at DATETIME   NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_login_failures_key (key_hash, created_at),
  KEY idx_login_failures_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE audit_log (
  id         BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id    BIGINT      NULL,                  -- NULL: aksi sistem/worker atau login gagal user tak dikenal
  action     VARCHAR(32) NOT NULL,              -- view | create | update | delete | export | login | login_failed | logout
  entity     VARCHAR(64) NOT NULL,              -- patient, user, ...
  entity_id  VARCHAR(64) NOT NULL DEFAULT '',
  ip         VARCHAR(45) NOT NULL DEFAULT '',
  request_id VARCHAR(32) NOT NULL DEFAULT '',
  detail     JSON        NULL,                  -- ID & nama field yang berubah; BUKAN isi data medis/password
  created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_audit_entity (entity, entity_id, created_at),
  KEY idx_audit_user (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- +goose Down
DROP TABLE audit_log;
DROP TABLE login_failures;
DROP TABLE users;
```

- Charset & collation ditulis eksplisit di setiap `CREATE TABLE`. Kalau tidak, tabel mengikuti default server, yang di instalasi MariaDB bawaan masih `latin1`.
- Username memakai `utf8mb4_unicode_ci`, jadi `Budi`, `budi`, dan `búdi` dianggap user yang sama. Karena itu kunci batas percobaan login adalah bentuk kanonik username menurut collation ini (`LoginThrottleKey`), bukan teks yang diketik (§4). Kalau collation `users.username` diganti, collation di query itu ikut diganti.
- `login_failures` hanya menyimpan hash kunci. Username yang salah ketik sering berisi password, jadi tidak disimpan mentah.
- `audit_log` hanya pernah di-`INSERT` oleh aplikasi. User DB aplikasi tidak diberi hak `UPDATE`/`DELETE` di tabel ini (SKILL §12.2).

## 2. Query sqlc

```sql
-- queries/auth.sql
-- name: GetUserForLogin :one
SELECT id, password_hash, is_active FROM users WHERE username = ?;

-- name: CreateUser :execlastid
INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?);

-- name: UpdateUserPassword :exec
UPDATE users SET password_hash = ? WHERE id = ?;

-- name: LoginThrottleKey :one
-- Bentuk kanonik username menurut collation users.username (WAJIB sama): semua varian
-- yang login ke akun yang sama ("budi", "Budi", "búdi") menghasilkan kunci yang sama,
-- juga untuk username yang tidak terdaftar. COLLATE ditaruh di kolom tabel turunan,
-- karena sqlc v1.30 diam-diam membuang parameter di dalam COLLATE / CONVERT ... USING.
SELECT WEIGHT_STRING(i.name COLLATE utf8mb4_unicode_ci) AS throttle_key
FROM (SELECT CAST(sqlc.arg(username) AS CHAR) AS name) AS i;

-- name: CountLoginFailures :one
SELECT COUNT(*) FROM login_failures
WHERE key_hash = ? AND created_at > DATE_SUB(NOW(), INTERVAL CAST(sqlc.arg(window_seconds) AS SIGNED) SECOND);

-- name: InsertLoginFailure :execlastid
INSERT INTO login_failures (key_hash) VALUES (?);

-- name: ListLoginFailureIDs :many
SELECT id FROM login_failures WHERE key_hash = ? LIMIT 1000;

-- name: ListOldLoginFailureIDs :many
SELECT id FROM login_failures
WHERE created_at < DATE_SUB(NOW(), INTERVAL CAST(sqlc.arg(window_seconds) AS SIGNED) SECOND)
LIMIT 1000;

-- name: DeleteLoginFailures :execrows
DELETE FROM login_failures WHERE id IN (sqlc.slice('ids'));
```

```sql
-- queries/audit.sql
-- name: InsertAudit :exec
INSERT INTO audit_log (user_id, action, entity, entity_id, ip, request_id, detail)
VALUES (?, ?, ?, ?, ?, ?, ?);
```

`EXPLAIN` `CountLoginFailures` memakai `idx_login_failures_key` (`range`, `Using index`), sedangkan `ListOldLoginFailureIDs` memakai `idx_login_failures_created`.

## 3. `internal/authz`

```go
// internal/authz/authz.go
package authz

// Perm = satu izin. Konstanta di kode, bukan string bebas di handler, supaya typo gagal compile.
type Perm string

const (
	PatientRead  Perm = "patient:read"
	PatientWrite Perm = "patient:write"
	UserManage   Perm = "user:manage"
	AuditRead    Perm = "audit:read"
)

// roles: peran → izin. Disimpan di kode (bukan tabel) supaya perubahan hak akses lewat
// review & ikut versi aplikasi. Role yang tidak ada di sini tidak punya izin apa pun.
var roles = map[string][]Perm{
	"admin":   {PatientRead, PatientWrite, UserManage, AuditRead},
	"dokter":  {PatientRead, PatientWrite},
	"perawat": {PatientRead},
	"kasir":   {PatientRead},
}

var grants = func() map[string]map[Perm]bool {
	g := make(map[string]map[Perm]bool, len(roles))
	for role, perms := range roles {
		g[role] = make(map[Perm]bool, len(perms))
		for _, p := range perms {
			g[role][p] = true
		}
	}
	return g
}()

// Can: deny by default.
func Can(role string, p Perm) bool { return grants[role][p] }

// ValidRole dipakai form user management sebelum menyimpan role ke DB.
func ValidRole(role string) bool {
	_, ok := grants[role]
	return ok
}
```

Pemakaian per route ada di §8. Izin per route **bukan** pengganti pembatasan data: kalau dokter hanya boleh melihat pasien di unitnya, filter itu tetap ditulis di query (`WHERE unit_id = ?`) dengan nilai dari user di context.

## 4. `internal/auth`: password & batas percobaan

```go
// internal/auth/password.go
package auth

import (
	"context"
	"errors"
	"runtime"
	"unicode/utf8"

	"golang.org/x/crypto/bcrypt"
)

const (
	bcryptCost = 12 // ±0,2–0,3 detik CPU per hash; naikkan seiring hardware, user lama di-rehash saat login
	// NIST SP 800-63B-4: minimal 15 karakter bila password satu-satunya faktor (8 bila ada MFA).
	// Tanpa aturan komposisi (wajib angka/simbol); lebih baik tolak password yang umum/bocor.
	MinPasswordLen = 15
	maxPasswordLen = 72 // batas bcrypt dalam byte; lebih dari ini ditolak, bukan dipotong diam-diam
)

var (
	ErrPasswordTooShort = errors.New("password terlalu pendek")
	ErrPasswordTooLong  = errors.New("password maksimal 72 byte")
)

// ValidatePassword dipanggil saat membuat user / ganti password, sebelum Hash.
func ValidatePassword(pw string) error {
	switch {
	case utf8.RuneCountInString(pw) < MinPasswordLen:
		return ErrPasswordTooShort
	case len(pw) > maxPasswordLen:
		return ErrPasswordTooLong
	}
	return nil
}

// Hasher membatasi jumlah bcrypt yang berjalan bersamaan. bcrypt sengaja berat; tanpa batas,
// banjir POST /login menghabiskan CPU dan semua endpoint lain ikut lambat.
type Hasher struct {
	sem   chan struct{}
	dummy []byte
}

func NewHasher() *Hasher {
	dummy, err := bcrypt.GenerateFromPassword([]byte("dummy-untuk-menyamakan-waktu"), bcryptCost)
	if err != nil {
		panic(err) // hanya gagal kalau cost tidak valid
	}
	return &Hasher{sem: make(chan struct{}, runtime.GOMAXPROCS(0)), dummy: dummy}
}

func (h *Hasher) Hash(ctx context.Context, pw string) (string, error) {
	if err := h.acquire(ctx); err != nil {
		return "", err
	}
	defer h.release()
	b, err := bcrypt.GenerateFromPassword([]byte(pw), bcryptCost)
	return string(b), err
}

// Verify: hash kosong (username tidak ditemukan) tetap dibandingkan dengan hash dummy,
// supaya waktu respons tidak membocorkan username mana yang terdaftar.
func (h *Hasher) Verify(ctx context.Context, hash, pw string) (bool, error) {
	if err := h.acquire(ctx); err != nil {
		return false, err
	}
	defer h.release()
	target := []byte(hash)
	if hash == "" {
		target = h.dummy
	}
	err := bcrypt.CompareHashAndPassword(target, []byte(pw))
	switch {
	case err == nil:
		return hash != "", nil
	case errors.Is(err, bcrypt.ErrMismatchedHashAndPassword), errors.Is(err, bcrypt.ErrPasswordTooLong):
		return false, nil
	default:
		return false, err // hash di DB rusak
	}
}

// NeedsRehash: hash dibuat dengan cost lama. Dicek setelah login sukses (password asli tersedia).
func NeedsRehash(hash string) bool {
	cost, err := bcrypt.Cost([]byte(hash))
	return err == nil && cost < bcryptCost
}

// acquire menunggu slot mengikuti deadline request (middleware Timeout), bukan selamanya.
func (h *Hasher) acquire(ctx context.Context) error {
	select {
	case h.sem <- struct{}{}:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (h *Hasher) release() { <-h.sem }
```

```go
// internal/auth/throttle.go
package auth

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/netip"
	"time"

	"app/internal/store"
)

const (
	throttleWindow  = 15 * time.Minute
	maxUserFailures = 5  // per akun per window
	maxIPFailures   = 30 // per IP per window; longgar karena satu klinik bisa keluar lewat satu IP NAT
)

// Throttle: batas percobaan login di MariaDB (tanpa Redis). Jendela geser: kegagalan
// lebih tua dari throttleWindow tidak dihitung, jadi blokir lepas sendiri.
type Throttle struct {
	q *store.Queries
}

func NewThrottle(db *sql.DB) *Throttle { return &Throttle{q: store.New(db)} }

// UserKey: kunci per username = bentuk kanonik menurut collation users.username, dihitung
// MariaDB. Collation _ci (juga mengabaikan aksen) membuat "budi", "Budi", dan "búdi" login
// ke akun yang sama; kalau kuncinya teks yang diketik, tiap varian mendapat jatah
// percobaan sendiri. Username tak dikenal diperlakukan sama persis, sehingga perilaku
// blokir tidak membocorkan apakah sebuah username terdaftar.
// Kunci di-hash: username yang salah ketik sering berisi password, jadi tidak disimpan mentah.
func (t *Throttle) UserKey(ctx context.Context, username string) ([]byte, error) {
	v, err := t.q.LoginThrottleKey(ctx, username)
	if err != nil {
		return nil, err
	}
	w, ok := v.([]byte) // sqlc tidak tahu tipe WEIGHT_STRING; driver mengembalikan []byte
	if !ok {
		return nil, fmt.Errorf("throttle key: tipe tak terduga %T", v)
	}
	h := sha256.Sum256(append([]byte("u:"), w...))
	return h[:], nil
}

// ipKey: IPv6 dihitung per /64, karena satu pelanggan biasanya memegang satu /64 utuh
// dan bisa berganti alamat di dalamnya untuk lolos dari batas per alamat.
func ipKey(ip string) []byte {
	if a, err := netip.ParseAddr(ip); err == nil && a.Is6() && !a.Is4In6() {
		if p, err := a.Prefix(64); err == nil {
			ip = p.String()
		}
	}
	h := sha256.Sum256([]byte("ip:" + ip))
	return h[:]
}

// Attempt = baris login_failures milik percobaan yang sedang berjalan.
type Attempt struct{ userRow, ipRow int64 }

// Begin dipanggil SEBELUM bcrypt. Percobaan dicatat lebih dulu sebagai kegagalan, BARU
// dihitung (hitungan sudah termasuk dirinya). Kalau dibalik (hitung lalu catat setelah
// bcrypt), ratusan request paralel lolos pengecekan bersamaan selagi hitungannya masih 0.
// Kedua INSERT sengaja tanpa transaksi: harus sudah commit, supaya terlihat oleh request
// paralel. Setiap request melihat semua catatan yang commit sebelum hitungannya, jadi
// paling banyak maxUserFailures percobaan per akun yang sampai ke bcrypt.
//
// Percobaan yang diblokir menghapus catatannya lagi. Tanpa itu, penyerang yang terus
// mencoba membuat akun target terkunci tanpa batas, bukan hanya selama throttleWindow.
// Kalau proses gagal di tengah (error DB, ctx habis), catatan tetap ada dan dihitung gagal.
func (t *Throttle) Begin(ctx context.Context, uk []byte, ip string) (a Attempt, blocked bool, err error) {
	if a.userRow, err = t.q.InsertLoginFailure(ctx, uk); err != nil {
		return a, false, err
	}
	if a.ipRow, err = t.q.InsertLoginFailure(ctx, ipKey(ip)); err != nil {
		return a, false, err
	}
	win := int64(throttleWindow / time.Second)
	nu, err := t.q.CountLoginFailures(ctx, store.CountLoginFailuresParams{KeyHash: uk, WindowSeconds: win})
	if err != nil {
		return a, false, err
	}
	ni, err := t.q.CountLoginFailures(ctx, store.CountLoginFailuresParams{KeyHash: ipKey(ip), WindowSeconds: win})
	if err != nil {
		return a, false, err
	}
	if nu > maxUserFailures || ni > maxIPFailures {
		_, err = t.q.DeleteLoginFailures(ctx, []int64{a.userRow, a.ipRow})
		return a, true, err
	}
	return a, false, nil
}

// Success menghapus semua kegagalan milik akun ini, ditambah catatan IP milik percobaan
// sukses ini saja. Kegagalan IP sebelumnya sengaja tidak di-reset: penyerang yang punya
// satu akun valid tidak bisa memakainya untuk me-reset batas IP.
// Gagal login tidak perlu memanggil apa pun: catatan dari Begin sudah menjadi kegagalannya.
func (t *Throttle) Success(ctx context.Context, a Attempt, uk []byte) error {
	ids, err := t.q.ListLoginFailureIDs(ctx, uk)
	if err != nil {
		return err
	}
	_, err = t.q.DeleteLoginFailures(ctx, append(ids, a.ipRow))
	return err
}

// HandleCleanup = job periodik: w.Every(jobs.KindLoginFailureCleanup, time.Hour).
// Pola SELECT id → DELETE by PK per 1000 (SKILL §6.4).
func (t *Throttle) HandleCleanup(ctx context.Context, _ json.RawMessage) error {
	for {
		ids, err := t.q.ListOldLoginFailureIDs(ctx, int64(throttleWindow/time.Second))
		if err != nil || len(ids) == 0 {
			return err
		}
		if _, err := t.q.DeleteLoginFailures(ctx, ids); err != nil {
			return err
		}
		if len(ids) < 1000 {
			return nil
		}
	}
}
```

## 5. `internal/auth`: handler login/logout

```go
// internal/auth/handler.go
package auth

import (
	"database/sql"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"strings"

	"app/internal/audit"
	"app/internal/middleware"
	"app/internal/render"
	"app/internal/session"
	"app/internal/store"
)

type Handler struct {
	q        *store.Queries
	sessions *session.Store
	hasher   *Hasher
	throttle *Throttle
	view     *render.Engine
	log      *slog.Logger
}

func NewHandler(db *sql.DB, sessions *session.Store, hasher *Hasher, view *render.Engine, log *slog.Logger) *Handler {
	return &Handler{q: store.New(db), sessions: sessions, hasher: hasher, throttle: NewThrottle(db), view: view, log: log}
}

func (h *Handler) Routes(mux *http.ServeMux, a *middleware.Auth) {
	mux.HandleFunc("GET /login", h.Form)
	mux.HandleFunc("POST /login", h.Login)
	mux.Handle("POST /logout", a.Require(http.HandlerFunc(h.Logout)))
}

type formData struct {
	Username, Error string
}

func (h *Handler) Form(w http.ResponseWriter, r *http.Request) {
	h.view.Render(w, r, http.StatusOK, "auth/login", formData{})
}

func (h *Handler) Login(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 4<<10)
	if err := r.ParseForm(); err != nil {
		http.Error(w, "form tidak valid", http.StatusBadRequest)
		return
	}
	ctx := r.Context()
	username := strings.TrimSpace(r.PostFormValue("username"))
	password := r.PostFormValue("password")
	ip := middleware.ClientIP(ctx)

	uk, err := h.throttle.UserKey(ctx, username) // sama untuk semua varian huruf/aksen
	if err != nil {
		h.fail(w, r, err)
		return
	}
	attempt, blocked, err := h.throttle.Begin(ctx, uk, ip) // dicatat SEBELUM bcrypt
	if err != nil {
		h.fail(w, r, err)
		return
	}
	if blocked {
		h.form(w, r, http.StatusTooManyRequests, username, "Terlalu banyak percobaan gagal. Coba lagi dalam 15 menit.")
		return
	}

	user, err := h.q.GetUserForLogin(ctx, username)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		h.fail(w, r, err)
		return
	}
	ok, err := h.hasher.Verify(ctx, user.PasswordHash, password) // hash "" bila user tidak ada
	if err != nil {
		h.fail(w, r, err)
		return
	}
	if !ok || !user.IsActive {
		// Satu pesan untuk username tak dikenal, password salah, dan user nonaktif.
		// Kegagalan sudah tercatat oleh Begin.
		if err := audit.Write(ctx, h.q, audit.Entry{Action: "login_failed", Entity: "user",
			EntityID: idString(user.ID), UserID: user.ID}); err != nil {
			h.fail(w, r, err)
			return
		}
		h.form(w, r, http.StatusUnprocessableEntity, username, "Username atau password salah.")
		return
	}

	if err := h.throttle.Success(ctx, attempt, uk); err != nil {
		h.fail(w, r, err)
		return
	}
	if err := h.sessions.Login(ctx, w, r, user.ID); err != nil {
		h.fail(w, r, err)
		return
	}
	if err := audit.Write(ctx, h.q, audit.Entry{Action: "login", Entity: "user",
		EntityID: idString(user.ID), UserID: user.ID}); err != nil {
		h.fail(w, r, err)
		return
	}
	if NeedsRehash(user.PasswordHash) { // best effort: gagal di sini tidak menggagalkan login
		if hash, err := h.hasher.Hash(ctx, password); err == nil {
			err = h.q.UpdateUserPassword(ctx, store.UpdateUserPasswordParams{PasswordHash: hash, ID: user.ID})
			if err != nil {
				h.log.WarnContext(ctx, "rehash password gagal", "user_id", user.ID, "err", err)
			}
		}
	}
	if r.Header.Get("HX-Request") == "true" {
		w.Header().Set("HX-Redirect", "/")
		w.WriteHeader(http.StatusNoContent)
		return
	}
	http.Redirect(w, r, "/", http.StatusSeeOther)
}

func (h *Handler) Logout(w http.ResponseWriter, r *http.Request) {
	user, _ := session.UserFrom(r.Context())
	if err := audit.Write(r.Context(), h.q, audit.Entry{Action: "logout", Entity: "user", EntityID: idString(user.ID)}); err != nil {
		h.fail(w, r, err)
		return
	}
	if err := h.sessions.Logout(r.Context(), w, r); err != nil {
		h.fail(w, r, err)
		return
	}
	http.Redirect(w, r, "/login", http.StatusSeeOther)
}

// form: request htmx menerima fragment form saja (status 422/429 di-swap oleh app.js, SKILL §5).
func (h *Handler) form(w http.ResponseWriter, r *http.Request, status int, username, msg string) {
	name := "auth/login"
	if r.Header.Get("HX-Request") == "true" {
		name = "auth/_form"
	}
	h.view.Render(w, r, status, name, formData{Username: username, Error: msg})
}

func (h *Handler) fail(w http.ResponseWriter, r *http.Request, err error) {
	h.log.ErrorContext(r.Context(), "login gagal diproses", "err", err)
	http.Error(w, "internal server error", http.StatusInternalServerError)
}

func idString(id int64) string {
	if id == 0 {
		return ""
	}
	return strconv.FormatInt(id, 10)
}
```

```html
<!-- internal/auth/views/login.html -->
{{define "title"}}Masuk{{end}}
{{define "content"}}{{template "_form.html" .}}{{end}}
```

```html
<!-- internal/auth/views/_form.html -->
<form id="login-form" method="post" action="/login" hx-post="/login" hx-target="this" hx-swap="outerHTML">
  {{with .Error}}<p class="form-error" role="alert">{{.}}</p>{{end}}
  <label>Username <input name="username" value="{{.Username}}" autocomplete="username" required></label>
  <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
  <button type="submit">Masuk</button>
</form>
```

Alurnya, dan alasan urutannya:

1. **Kunci throttle = bentuk kanonik username** menurut collation (`Throttle.UserKey`). Varian huruf besar/kecil atau aksen dari satu username tidak mendapat jatah percobaan sendiri-sendiri, baik username itu terdaftar maupun tidak.
2. **Catat percobaan, baru hitung** (`Throttle.Begin`), sebelum lookup user dan bcrypt. Urutan ini yang membuat request paralel tidak bisa lolos bersamaan: paling banyak 5 percobaan per akun yang sampai ke bcrypt. Percobaan yang diblokir menghapus catatannya lagi dan tidak memakan CPU bcrypt.
3. **bcrypt selalu jalan**, juga untuk username yang tidak ada (memakai hash dummy). Dengan begitu waktu respons tidak membocorkan username mana yang terdaftar (terukur 203 ms vs 212 ms).
4. **Satu pesan error** untuk username tak dikenal, password salah, dan user nonaktif. Catatan dari langkah 2 menjadi kegagalannya.
5. Sukses: kegagalan milik akun dan catatan IP percobaan ini dihapus, session dibuat (rotasi), audit `login`, lalu hash di-upgrade kalau cost-nya lama.

Menambah atau mengubah user (form admin, perintah bootstrap) wajib memanggil `auth.ValidatePassword` lalu `hasher.Hash`. Setelah ganti password atau menonaktifkan user, panggil `q.DeleteUserSessions(ctx, userID)` supaya semua perangkat ter-logout.

## 6. `internal/audit`

```go
// internal/audit/audit.go
package audit

import (
	"context"
	"database/sql"
	"encoding/json"

	"app/internal/middleware"
	"app/internal/session"
	"app/internal/store"
)

type Entry struct {
	Action   string // "view", "create", "update", "delete", "export", "login", "login_failed", "logout"
	Entity   string // "patient", "user", ...
	EntityID string
	UserID   int64 // opsional; default diambil dari session di ctx (login mengisinya sendiri)
	Detail   any   // opsional: ID & nama field yang berubah. JANGAN isi data medis atau password
}

// Write menulis satu baris audit secara sinkron.
//   - Mutasi: panggil dengan q dari InTx yang sama, jadi audit ikut commit/rollback bersama datanya.
//   - Akses baca data sensitif: panggil dengan store.New(db) SEBELUM render; kalau gagal, balas 500.
//
// User, IP, dan request ID diambil dari ctx (middleware Require & RequestLog).
func Write(ctx context.Context, q *store.Queries, e Entry) error {
	var detail json.RawMessage
	if e.Detail != nil {
		b, err := json.Marshal(e.Detail)
		if err != nil {
			return err
		}
		detail = b
	}
	userID := e.UserID
	if u, ok := session.UserFrom(ctx); ok && userID == 0 {
		userID = u.ID
	}
	return q.InsertAudit(ctx, store.InsertAuditParams{
		UserID:    sql.NullInt64{Int64: userID, Valid: userID != 0},
		Action:    e.Action,
		Entity:    e.Entity,
		EntityID:  e.EntityID,
		Ip:        middleware.ClientIP(ctx),
		RequestID: middleware.RequestID(ctx),
		Detail:    detail,
	})
}
```

Contoh di service dan handler:

```go
// mutasi: audit di transaksi yang sama dengan datanya
err := database.InTx(ctx, s.db, func(q *store.Queries) error {
	if err := q.UpdatePatientAddress(ctx, p); err != nil {
		return err
	}
	return audit.Write(ctx, q, audit.Entry{Action: "update", Entity: "patient",
		EntityID: strconv.FormatInt(p.ID, 10), Detail: map[string]any{"fields": []string{"alamat"}}})
})

// baca data sensitif: audit dulu, baru render. Gagal tercatat = gagal ditampilkan.
if err := audit.Write(r.Context(), h.q, audit.Entry{Action: "view", Entity: "patient", EntityID: id}); err != nil {
	h.fail(w, r, err)
	return
}
h.view.Render(w, r, http.StatusOK, "patients/detail", patient)
```

## 7. Middleware `RequestLog` & `SecurityHeaders`

```go
// internal/middleware/requestlog.go
package middleware

import (
	"context"
	"crypto/rand"
	"log/slog"
	"net/http"
	"net/netip"
	"strings"
	"time"
)

type reqInfo struct{ id, ip string }

type infoKey struct{}

// RequestID dan ClientIP dibaca handler/audit dari context. Kosong di luar request HTTP (worker).
func RequestID(ctx context.Context) string { i, _ := ctx.Value(infoKey{}).(reqInfo); return i.id }
func ClientIP(ctx context.Context) string  { i, _ := ctx.Value(infoKey{}).(reqInfo); return i.ip }

// RequestLog: lapisan paling luar. Membuat request ID, menentukan IP klien, lalu mencatat
// satu baris log per request. trusted = CIDR reverse proxy milik sendiri (TRUSTED_PROXIES).
func RequestLog(log *slog.Logger, trusted []netip.Prefix) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			info := reqInfo{id: rand.Text()[:16], ip: clientIP(r, trusted)}
			w.Header().Set("X-Request-ID", info.id)
			rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
			ctx := context.WithValue(r.Context(), infoKey{}, info)
			next.ServeHTTP(rec, r.WithContext(ctx))
			log.InfoContext(ctx, "request", "method", r.Method, "path", r.URL.Path,
				"status", rec.status, "duration_ms", time.Since(start).Milliseconds(), "ip", info.ip)
		})
	}
}

// clientIP: X-Forwarded-For hanya dipercaya kalau koneksi datang dari proxy sendiri.
// Dibaca dari kanan; entri paling kanan yang bukan proxy terpercaya = IP klien.
// Entri kiri bisa dipalsukan klien, jadi jangan pernah ambil yang paling kiri.
func clientIP(r *http.Request, trusted []netip.Prefix) string {
	ap, err := netip.ParseAddrPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	ip := ap.Addr().Unmap()
	if !inPrefixes(ip, trusted) {
		return ip.String()
	}
	hops := strings.Split(strings.Join(r.Header.Values("X-Forwarded-For"), ","), ",")
	for i := len(hops) - 1; i >= 0; i-- {
		a, err := netip.ParseAddr(strings.TrimSpace(hops[i]))
		if err != nil {
			break // entri rusak: berhenti, pakai hop terakhir yang valid
		}
		ip = a.Unmap()
		if !inPrefixes(ip, trusted) {
			break
		}
	}
	return ip.String()
}

func inPrefixes(ip netip.Addr, ps []netip.Prefix) bool {
	for _, p := range ps {
		if p.Contains(ip) {
			return true
		}
	}
	return false
}

type statusRecorder struct {
	http.ResponseWriter
	status int
	wrote  bool
}

func (r *statusRecorder) WriteHeader(code int) {
	if !r.wrote {
		r.status, r.wrote = code, true
	}
	r.ResponseWriter.WriteHeader(code)
}

func (r *statusRecorder) Write(b []byte) (int, error) {
	r.wrote = true
	return r.ResponseWriter.Write(b)
}

// Unwrap: http.ResponseController (Flush, SetWriteDeadline per route) tetap bekerja.
func (r *statusRecorder) Unwrap() http.ResponseWriter { return r.ResponseWriter }

// LogHandler membungkus handler slog supaya setiap log ber-context membawa request_id:
// slog.New(middleware.LogHandler(slog.NewJSONHandler(os.Stdout, nil))).
func LogHandler(h slog.Handler) slog.Handler { return ctxHandler{h} }

type ctxHandler struct{ slog.Handler }

func (h ctxHandler) Handle(ctx context.Context, rec slog.Record) error {
	if id := RequestID(ctx); id != "" {
		rec.AddAttrs(slog.String("request_id", id))
	}
	return h.Handler.Handle(ctx, rec)
}

func (h ctxHandler) WithAttrs(as []slog.Attr) slog.Handler {
	return ctxHandler{h.Handler.WithAttrs(as)}
}
func (h ctxHandler) WithGroup(name string) slog.Handler { return ctxHandler{h.Handler.WithGroup(name)} }
```

```go
// internal/middleware/security.go
package middleware

import "net/http"

// csp: semua script & style hanya dari /static milik sendiri. Tanpa 'unsafe-inline' dan
// 'unsafe-eval', sehingga HTML hasil injeksi tidak bisa menjalankan script.
// Syaratnya: Alpine build CSP, htmx allowEval=false, tanpa <script>/style="" inline (SKILL §5.1).
const csp = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
	"font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; " +
	"form-action 'self'; frame-ancestors 'none'"

// SecurityHeaders dipasang global. hsts hanya true kalau aplikasi diakses lewat HTTPS
// (sama dengan CookieSecure); HSTS di HTTP biasa diabaikan browser tapi menyesatkan.
func SecurityHeaders(hsts bool) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			h := w.Header()
			h.Set("Content-Security-Policy", csp)
			h.Set("X-Content-Type-Options", "nosniff")
			h.Set("X-Frame-Options", "DENY") // browser lama yang belum paham frame-ancestors
			h.Set("Referrer-Policy", "strict-origin-when-cross-origin")
			h.Set("Cross-Origin-Opener-Policy", "same-origin")
			if hsts {
				h.Set("Strict-Transport-Security", "max-age=31536000") // + includeSubDomains bila semua subdomain HTTPS
			}
			next.ServeHTTP(w, r)
		})
	}
}
```

## 8. Wiring

```go
// cmd/server/main.go (potongan, lengkapnya di struktur-project §5.1)
log := slog.New(middleware.LogHandler(slog.NewJSONHandler(os.Stdout, nil)))

sessions := session.NewStore(q, cfg.SessionTTL, cfg.SessionMaxAge, cfg.CookieSecure)
auth := middleware.NewAuth(sessions, log)
authh.NewHandler(db, sessions, authh.NewHasher(), view, log).Routes(mux, auth) // import authh "app/internal/auth"

handler := middleware.Chain(mux,
	middleware.RequestLog(log, cfg.TrustedProxies), // request ID + IP klien, paling luar
	middleware.Recover(log),
	middleware.SecurityHeaders(cfg.CookieSecure), // HSTS hanya bila HTTPS
	csrf.Handler,
	middleware.Timeout(cfg.RequestTimeout),
)
```

```go
// internal/patients/handler.go: izin per route
func (h *Handler) Routes(mux *http.ServeMux, a *middleware.Auth) {
	mux.Handle("GET /patients", a.Perm(authz.PatientRead)(http.HandlerFunc(h.List)))
	mux.Handle("GET /patients/{id}", a.Perm(authz.PatientRead)(http.HandlerFunc(h.Detail)))
	mux.Handle("POST /patients", a.Perm(authz.PatientWrite)(http.HandlerFunc(h.Create)))
}
```

```go
// cmd/worker/main.go: bersihkan login_failures lama
throttle := authh.NewThrottle(db)
w.Handle(jobs.KindLoginFailureCleanup, throttle.HandleCleanup)
w.Every(jobs.KindLoginFailureCleanup, time.Hour)
```

## 9. Frontend yang lolos CSP

CSP di §7 tidak mengizinkan `'unsafe-inline'` maupun `'unsafe-eval'`. Agar HTMX dan Alpine tetap berjalan:

```html
<!-- web/layout/base.html, di dalam <head> -->
<meta name="htmx-config" content='{"allowEval":false,"allowScriptTags":false,"includeIndicatorStyles":false,"historyCacheSize":0}'>
<link rel="stylesheet" href="/static/css/app.css?v={{version}}">
<script src="/static/js/htmx.min.js?v={{version}}" defer></script>
<script src="/static/js/app.js?v={{version}}" defer></script>
<script src="/static/js/alpine-components.js?v={{version}}" defer></script>
<script src="/static/js/alpine-csp.min.js?v={{version}}" defer></script> <!-- dari paket @alpinejs/csp -->
```

```css
/* static/css/app.css: pengganti style indikator yang biasanya disuntikkan htmx */
.htmx-indicator { opacity: 0; }
.htmx-request .htmx-indicator, .htmx-request.htmx-indicator { opacity: 1; transition: opacity 200ms ease-in; }
```

```js
// static/js/app.js: 429 (login diblokir) ikut di-swap seperti error validasi
document.addEventListener('htmx:beforeSwap', (evt) => {
  const s = evt.detail.xhr.status;
  if (s === 400 || s === 422 || s === 429) { evt.detail.shouldSwap = true; evt.detail.isError = false; }
});
```

| Dilarang (diblokir CSP atau build CSP) | Pengganti |
|---|---|
| `<script>` inline, `onclick=`, `style="..."` | file di `/static`, class CSS |
| `hx-on:*`, filter event `hx-trigger="click[ctrlKey]"`, `hx-vals='js:...'` | event `HX-Trigger` + `Alpine.data` |
| Arrow function, template literal, `console`/`document`/`Math` di atribut Alpine | method di `Alpine.data` |
| `x-html` dengan isi dari user | `x-text`, atau render di server lewat `html/template` |

Ekspresi sederhana di atribut tetap boleh (`x-data="{ open: false }"`, `@click="open = !open"`, `x-show="count > 3"`).

## 10. Hasil Verifikasi

Kode di dokumen ini (salinan persis dari kode yang diuji) di-generate dengan sqlc v1.30 (`sqlc diff` bersih), lolos `go vet` + `gofmt`, dan diuji `go test -race` terhadap MariaDB 10.11.13. Toolchain yang dipakai adalah Go 1.26.0, karena `golang.org/x/crypto` v0.58 mewajibkan Go ≥ 1.26. Driver yang dipakai `go-sql-driver/mysql` v1.10.1.

| Skenario | Hasil |
|---|---|
| Proses Go `TZ=UTC`, server DB berzona WIB, `Open` dengan `Location=Asia/Jakarta` | `@@session.time_zone = '+07:00'`, `NOW()` terbaca selisih < 5 detik dari `time.Now()` |
| Pembanding: DSN lama `loc=Local` di kondisi yang sama | `NOW()` terbaca **bergeser 7 jam** |
| bcrypt cost 12 | ±0,2 detik per verifikasi (±2,7 detik dengan `-race`) |
| Verifikasi username tak dikenal vs password salah | 212 ms vs 203 ms (tidak bisa dibedakan dari waktu) |
| Password > 72 byte saat login / saat validasi | ditolak tanpa error / `ErrPasswordTooLong` |
| Semua slot `Hasher` penuh, ctx dibatalkan | `Verify` langsung kembali dengan `context.Canceled` |
| 5 password salah untuk satu username (huruf besar/kecil berbeda) | percobaan ke-6 → 429 **walau password benar**, tanpa bcrypt (< 100 ms) |
| Kegagalan lewat 15 menit | blokir lepas, login sukses, kegagalan username terhapus, kegagalan IP tetap |
| 30 kegagalan dari satu IP dengan username berbeda-beda | IP itu → 429; IP lain tetap bisa login |
| Username tak dikenal / user nonaktif dengan password benar | 422 dengan pesan yang sama |
| `X-Forwarded-For: 1.1.1.1, 203.0.113.9` dari proxy terpercaya | IP klien = `203.0.113.9` (entri kiri palsu diabaikan); 11 kasus `clientIP` lain lulus |
| Audit login/login_failed/logout | tercatat dengan `user_id`, IP, `request_id` |
| `audit.Write` di `InTx` yang di-rollback / commit | ikut batal / tersimpan; `Detail` nil → `NULL` |
| Hash cost 10 di DB, login sukses | hash diganti cost 12 |
| Role `kasir`: `GET /patients` (read) / `POST /patients` (write) | 200 / 403 |
| Role diubah di DB / role tak dikenal | berlaku di request berikutnya / 403 di semua route |
| User dinonaktifkan | request berikutnya → redirect `/login` (HTMX: 401 + `HX-Redirect`) |
| Halaman ber-session / halaman publik | `Cache-Control: no-store` / tanpa `no-store`; CSP & header keamanan di keduanya, HSTS hanya bila HTTPS |
| Cookie di HTTPS / HTTP | `__Host-session_id` Secure / `session_id` |
| Log | setiap baris ber-context membawa `request_id` |
| `cleanup` login_failures | hanya baris lebih tua dari jendela yang terhapus |

**Revisi review 2026-10-10** (throttle "catat dulu, baru hitung", kunci kanonik menurut collation, IPv6 per /64, collation eksplisit). Kode di atas diekstrak langsung dari dokumen ini, `sqlc generate` + `sqlc diff` bersih, lolos `go vet` + `gofmt`, dan diuji `go test -race` dengan Go 1.27.2, sqlc v1.30.0, dan MariaDB 10.11.13:

| Skenario | Hasil |
|---|---|
| MariaDB 10.11 bawaan tanpa konfigurasi | `character_set_server = latin1`, jadi charset wajib ditulis di `CREATE TABLE` |
| `búdi` / `BÙDÎ` vs user `budi` di `utf8mb4_unicode_ci`, `utf8mb4_general_ci`, dan `latin1` default | ketiganya cocok (login ke akun yang sama) |
| **Algoritma lama** (hitung → bcrypt → catat): 200 percobaan paralel ke satu akun | **200 dari 200** sampai ke bcrypt (batas seharusnya 5) |
| Algoritma baru: 200 percobaan paralel dari 200 IP berbeda, diulang 3× | 0–1 sampai ke bcrypt per gelombang, sisanya 429; ditambah 20 percobaan berurutan, total yang sampai ke bcrypt **tepat 5**; password benar sesudahnya → 429 |
| 5 gagal dengan `budi`, `Budi`, `búdi`, `bùdi`, `büdi`, lalu `bûdi` / `BÚDÍ` | 429 (satu jatah per akun) |
| 5 gagal dengan username **tak terdaftar** `tidakada`, lalu `tidakadá` / `TÏDAKADA` | 429, sama seperti username terdaftar (tidak ada oracle keberadaan akun) |
| `WEIGHT_STRING` `utf8mb4_unicode_ci`: `budi` vs `BÚDÎ` / `bud` | sama / beda |
| sqlc v1.30: parameter di dalam `COLLATE` atau `CONVERT(... USING ...)` | **dibuang diam-diam** (fungsi Go tanpa argumen); bentuk tabel turunan di `LoginThrottleKey` benar |
| 10 percobaan yang diblokir | tidak menambah baris `login_failures` |
| 3 gagal lalu sukses dari IP yang sama | kegagalan akun terhapus; kegagalan IP tetap 3 (catatan percobaan sukses ikut dihapus) |
| `2001:db8:1:2::1` vs `2001:db8:1:2:ffff:…` / `2001:db8:1:3::1`; dua alamat IPv4 | kunci sama / beda; IPv4 tetap per alamat |

Di bawah serangan paralel, percobaan sah yang datang bersamaan bisa ikut mendapat 429, karena catatan percobaan yang sedang berjalan ikut terhitung sesaat. Ini sengaja: lebih baik salah blokir sebentar daripada meloloskan tebakan.
