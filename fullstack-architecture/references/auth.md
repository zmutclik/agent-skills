# Auth: Detail

Aturan inti ada di SKILL.md §5. File acuan: `template/app/auth/`.

## 1. Session

- Tabel `user_sessions` dengan PK = SHA-256 session ID; nilai asli hanya ada di cookie.
- **`last_seen_at` di-throttle**: diperbarui (dan `expires_at` diperpanjang) maksimal sekali per `TOUCH_INTERVAL` (5 menit), bukan UPDATE setiap request.
- **Dua batas waktu**: idle timeout `SESSION_TTL` (7 hari, diperpanjang saat aktif) dan batas mutlak `SESSION_MAX_AGE` (30 hari sejak login, `expires_at` tidak pernah melewati `created_at + SESSION_MAX_AGE`). Cookie dipasang sekali dengan `max_age=SESSION_MAX_AGE` dan tidak diperbarui; server yang memutuskan kapan session berakhir. Jangan set `max_age` cookie = idle timeout: browser membuang cookie walau session di DB masih diperpanjang.
- `role` disalin ke session agar tidak perlu join setiap request; `change_role` menghapus semua session user tersebut.
- Request HTMX yang belum login mendapat `401` + `HX-Redirect: /login`; browser biasa mendapat redirect 303.
- **Pembersihan**: `python -m app.auth.cleanup` menghapus session kedaluwarsa dan catatan login lama (per batch). Jadwalkan tiap jam (cron/systemd timer/CronJob). Dengan addon jobs, fungsi yang sama berjalan otomatis lewat `@maintenance`.

## 2. Manajemen user

- `change_role` dan `change_password` menghapus semua session user tersebut dalam transaksi yang sama. `change_role` mengembalikan `False` bila user tidak ada.
- **Kebijakan di service**: `create_user`/`change_password` memanggil `validate_password` (min. `PASSWORD_MIN_LENGTH` = 8, `InvalidPassword`), `create_user`/`change_role` memanggil `validate_role` (hanya nilai di `ROLES`, `app/auth/models.py`). Form ganti password yang ditambahkan nanti otomatis ikut aturan ini; tangkap `InvalidPassword` di router untuk pesan 422.
- Admin pertama dan reset password dari terminal (`app/auth/cli.py`):
  ```bash
  python -m app.auth.cli create-user admin --role admin
  python -m app.auth.cli set-password admin
  ```
  Password dibaca dari prompt (dua kali), atau satu baris stdin bila bukan terminal; tidak pernah dari argumen (tercatat di riwayat shell dan daftar proses). `--role` hanya menerima nilai `ROLES` (salah ketik ditolak, bukan membuat user tanpa hak).

## 3. CSRF

- Token = HMAC(`SECRET_KEY`, hash session ID); dicek `require_csrf` untuk semua method non-GET.
- HTMX mengirimnya lewat `hx-headers` di `<body>`; form biasa lewat hidden input `csrf_token`.
- Form login (belum ada session) memakai double-submit cookie (`login_csrf`, `SameSite=Strict`, `path=/login`) tanpa menulis ke DB.

## 4. Login

- Selalu buat session ID baru (cegah session fixation).
- argon2 dijalankan di threadpool **dan** di luar transaksi, dibatasi `PASSWORD_HASH_CONCURRENCY` per proses (default 4). Tiap hash memakai ±64 MiB; tanpa batas, lonjakan login memakai seluruh threadpool (40 thread) dan RAM.
- Username yang tidak ada tetap diverifikasi terhadap hash dummy, agar waktu respons tidak membocorkan username valid.
- Username panjang berapa pun aman: teks username tidak pernah di-INSERT ke `login_failures` (mode strict MariaDB menolak nilai terlalu panjang → 500).

## 5. Rate limit login (dua lapis)

1. `limit_req` di nginx, hanya untuk `POST /login` (`deployment.md` §5).
2. Tabel `login_failures` dalam `LOGIN_FAILURE_WINDOW`, dua batas:
   - per pasangan (user, IP): `LOGIN_MAX_FAILURES` (5);
   - per IP untuk semua username: `LOGIN_MAX_FAILURES_PER_IP` (50), longgar karena satu kantor di balik NAT berbagi IP.
   - **Kunci IP** = `rate_limit_key()`: IPv4 apa adanya, IPv6 dipotong ke prefix /64. Satu pelanggan IPv6 biasanya memegang seluruh /64; dihitung per alamat, penyerang cukup berganti alamat untuk menembus kedua batas. `limit_req` nginx tetap per alamat (nginx tidak bisa mengelompokkan per prefix), jadi lapis app inilah batas yang sebenarnya.
   - **Disimpan `user_id` hasil lookup, bukan teks username**: varian yang dianggap sama oleh collation `_ci` (`Alice`/`alice`) tetap satu hitungan, dan password yang tidak sengaja diketik di kolom username tidak tersimpan. Username yang tidak ada → `user_id` NULL, hanya terhitung per IP.
   - **Tidak ada batas per user saja**: dengan batas itu, siapa pun bisa mengunci akun korban dari IP mana pun (DoS lockout). Konsekuensinya, tebakan terdistribusi dari banyak IP ke satu akun hanya dibatasi per IP; andalkan argon2 + kebijakan password, atau tambahkan MFA bila ancaman itu nyata.
   - Percobaan dicatat dan di-commit **sebelum** dihitung. Dengan hitung-dulu-catat-belakangan, request paralel membaca hitungan lama dan lolos semua.
   - Percobaan yang ditolak ikut tercatat (serangan berkelanjutan memperpanjang blokir, tapi hanya untuk pasangan/IP penyerang); baris pasangan (user, IP) dihapus bila login berhasil.
   - IP hanya benar bila proxy menimpa `X-Forwarded-For` dan uvicorn hanya mempercayai IP proxy (`deployment.md` §1).

## 6. Addon `api-tokens` (token M2M)

Pasang hanya bila ada client mesin (ERP, integrasi, cron eksternal) yang memanggil `/api/...`.

```bash
cp -r <skill>/addons/api-tokens/. ./
uv run alembic revision --autogenerate -m "add api_tokens"   # review, lalu upgrade head
```

Tidak ada file lain yang perlu diedit (auto-discovery). Menerbitkan dan mencabut token dari terminal:

```bash
python -m app.api_tokens.cli create erp --scopes "items:read items:write" --expires-days 365   # stdout = token
python -m app.api_tokens.cli list
python -m app.api_tokens.cli revoke 3
```

Token mentah hanya dicetak sekali (stdout, bisa dipipe ke secret manager); `list` tidak pernah menampilkannya. Pakai `--expires-days` dan rotasi token secara berkala.

Isinya:
- `api_tokens` menyimpan hash + `scopes` (dipisah spasi) + `expires_at`/`revoked_at`; pencabutan langsung berlaku.
- `require_scope("items:read")` sebagai dependency router `/api/...` (error handler membalas JSON untuk path `/api/`).
- `last_used_at` di-throttle seperti session; dependency memakai session DB sendiri.
- Test: `tests/test_api_tokens.py` (hash, scope, pencabutan, kedaluwarsa, CLI).

Mobile/SPA beda domain: JWT, hanya saat client itu benar-benar ada.
