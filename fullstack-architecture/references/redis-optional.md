# Kapan Menambah Redis (Opsional)

Redis **bukan** bagian stack default. Tambahkan hanya bila salah satu pemicu ini **terukur** di metrics, bukan sekadar diperkirakan:

| Pemicu | Solusi dengan Redis |
|---|---|
| (Addon jobs) Lag antrean tinggi atau throughput job ratusan/detik, sehingga polling tabel `jobs` membebani DB | Pindahkan job queue ke Arq |
| Lookup session/token mendominasi beban DB | Pindahkan session ke Redis (TTL) |
| Query mahal yang dibaca berulang dan tidak bisa diatasi index | Cache bersama dengan TTL |
| Rate limiting / counter dengan RPS tinggi | `INCR` + `EXPIRE` |
| Broadcast antar-instance (SSE/WebSocket) | Pub/sub |

## Aturan saat Redis ditambahkan

- **Satu klien per proses**: `arq.connections.ArqRedis` di atas `redis.asyncio.BlockingConnectionPool(max_connections=..., timeout=...)`, dibuat di lifespan dan dipakai untuk cache, session, sekaligus enqueue. Jangan buat pool kedua dengan `arq.create_pool()`, karena pool itu tidak punya batas koneksi.
- **`maxmemory-policy noeviction`** bila Redis memegang antrean atau session. Dengan kebijakan `*-lru`, data job yang masih antre dan session bisa terhapus tanpa error. Semua key cache wajib punya TTL, dan memori Redis wajib dipantau.
- **Instance Redis cache terpisah** (`allkeys-lru`) bila volume cache besar. Memisahkan DB index (`/0`, `/1`) tidak cukup, karena eviction policy berlaku untuk seluruh instance.
- **Atomisitas hilang**: enqueue ke Redis tidak lagi atomik dengan transaksi DB. Pertahankan baris outbox di tabel `jobs` lalu teruskan ke Arq, atau tambah penanda status + re-enqueue periodik. Bila memakai `_job_id` Arq untuk dedup, `enqueue_job` mengembalikan `None` selama hasil job lama belum kedaluwarsa (`keep_result`).
- **Pencabutan session/token**: bila session atau token di-cache di Redis, hapus key-nya di proses yang sama saat dicabut.
