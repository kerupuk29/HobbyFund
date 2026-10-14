import hashlib
import io
import re
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import pytesseract
from PIL import Image, ImageOps, ImageEnhance
from supabase import create_client, Client

RATE_PER_HOUR = 5000

st.set_page_config(page_title="HobbyFund", page_icon="🪙", layout="wide")

st.markdown("""
<style>
    .block-container {max-width: 1100px; padding-top: 2rem; padding-bottom: 3rem;}
    [data-testid="stMetric"] {border: 1px solid rgba(128,128,128,.25); padding: 1rem; border-radius: 14px;}
</style>
""", unsafe_allow_html=True)

def get_supabase() -> Client:
    url = st.secrets.get("SUPABASE_URL", "")
    key = st.secrets.get("SUPABASE_KEY", "")
    if not url or not key:
        st.error("Supabase belum dikonfigurasi. Tambahkan SUPABASE_URL dan SUPABASE_KEY di Streamlit Secrets.")
        st.info("Ikuti bagian setup di README.md, lalu refresh aplikasi.")
        st.stop()
    return create_client(url, key)

def money(value):
    return "Rp" + f"{int(value):,}".replace(",", ".")

def parse_duration(text: str):
    """Return detected duration in minutes, or None."""
    t = text.lower().replace(",", ".")
    # HH:MM:SS or MM:SS, as commonly shown by timers and activity apps
    candidates = []
    for m in re.finditer(r"(?<!\d)(\d{1,2}):([0-5]\d):([0-5]\d)(?!\d)", t):
        h, minute, sec = map(int, m.groups())
        total = h * 60 + minute + sec / 60
        if 0 < total <= 24 * 60:
            candidates.append((total, m.start()))
    for m in re.finditer(r"(?<!\d)(\d{1,3}):([0-5]\d)(?!\d)", t):
        mm, sec = map(int, m.groups())
        total = mm + sec / 60
        if 0 < total <= 24 * 60:
            candidates.append((total, m.start()))
    # Explicit units: 1 h 20 m, 1 jam 20 menit, 45 min, 2 hours
    unit_patterns = [
        r"(?:(\d+(?:\.\d+)?)\s*(?:jam|hours?|hrs?|h)\b)?\s*(?:(\d+(?:\.\d+)?)\s*(?:menit|minutes?|mins?|min|m)\b)",
        r"(\d+(?:\.\d+)?)\s*(?:jam|hours?|hrs?|h)\b",
    ]
    for pat in unit_patterns:
        for m in re.finditer(pat, t):
            vals = [g for g in m.groups() if g is not None]
            if not vals:
                continue
            if len(vals) == 2:
                total = float(vals[0]) * 60 + float(vals[1])
            else:
                total = float(vals[0]) * (60 if re.search(r"(?:jam|hours?|hrs?|h)\b", m.group(0)) else 1)
            if 0 < total <= 24 * 60:
                candidates.append((total, m.start()))
    if not candidates:
        return None
    # Prefer longest plausible duration; OCR can capture incidental smaller times too.
    return int(round(max(candidates, key=lambda x: x[0])[0]))

def run_ocr(image_bytes: bytes):
    image = Image.open(io.BytesIO(image_bytes))
    image = ImageOps.exif_transpose(image).convert("RGB")
    image = ImageOps.grayscale(image)
    image = ImageEnhance.Contrast(image).enhance(2)
    image = image.resize((image.width * 2, image.height * 2))
    # PSM 11 works reasonably for varied app screenshots.
    return pytesseract.image_to_string(image, config="--psm 11")

def load_transactions(sb):
    response = sb.table("transactions").select("*").order("created_at", desc=True).limit(500).execute()
    rows = response.data or []
    df = pd.DataFrame(rows)
    if not df.empty:
        df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce", utc=True).dt.tz_convert("Asia/Jakarta")
    return df

def get_balance(sb):
    # Paginate to ensure balance isn't limited to the latest 500 transactions.
    total = 0
    start = 0
    while True:
        result = sb.table("transactions").select("amount").range(start, start + 999).execute()
        batch = result.data or []
        total += sum(int(row.get("amount", 0)) for row in batch)
        if len(batch) < 1000:
            break
        start += 1000
    return total

sb = get_supabase()

st.title("🪙 HobbyFund")
st.caption("Produktif dulu, baru reward buat hobi. Tarif dasar: Rp5.000 per jam produktif.")

try:
    balance = get_balance(sb)
    df = load_transactions(sb)
except Exception as exc:
    st.error("Gagal terhubung ke database. Periksa konfigurasi Supabase dan SQL schema di README.")
    st.exception(exc)
    st.stop()

earned = 0
spent = 0
if not df.empty:
    earned = int(df.loc[df["amount"] > 0, "amount"].sum())
    spent = int(-df.loc[df["amount"] < 0, "amount"].sum())

m1, m2, m3 = st.columns(3)
m1.metric("Saldo reward", money(balance))
m2.metric("Total reward didapat", money(earned))
m3.metric("Total dipakai untuk hobi", money(spent))

tab_claim, tab_spend, tab_history, tab_settings = st.tabs(
    ["➕ Tambah aktivitas", "🛍️ Tukar reward", "📜 Riwayat", "ℹ️ Cara kerja"]
)

with tab_claim:
    st.subheader("Klaim aktivitas produktif")
    st.write("Upload screenshot timer, Strava, atau aplikasi aktivitas. OCR akan mencoba membaca durasi; cek hasilnya sebelum mengonfirmasi.")
    uploaded = st.file_uploader("Screenshot bukti aktivitas", type=["png", "jpg", "jpeg", "webp"], key="proof")
    if uploaded:
        image_bytes = uploaded.getvalue()
        digest = hashlib.sha256(image_bytes).hexdigest()
        st.image(image_bytes, caption="Bukti yang akan dicatat", use_container_width=True)
        duplicate = False
        try:
            dup = sb.table("transactions").select("id,activity,created_at").eq("screenshot_hash", digest).limit(1).execute()
            duplicate = bool(dup.data)
        except Exception:
            st.warning("Pengecekan duplikat belum bisa dilakukan. Pastikan SQL schema di README sudah dijalankan.")
        if duplicate:
            st.error("Screenshot ini sudah pernah diklaim. Klaim diblokir untuk mencegah reward ganda.")
        else:
            with st.spinner("Membaca teks screenshot..."):
                try:
                    ocr_text = run_ocr(image_bytes)
                except Exception as exc:
                    ocr_text = ""
                    st.warning(f"OCR gagal membaca gambar: {exc}")
            detected = parse_duration(ocr_text) if ocr_text else None
            st.markdown("**Teks yang terbaca OCR**")
            st.text_area("Hasil OCR (bisa diperiksa)", value=ocr_text or "Tidak ada teks yang terbaca.", height=110, disabled=True)
            if detected:
                st.success(f"Durasi yang terdeteksi: {detected // 60} jam {detected % 60} menit")
            else:
                st.warning("Durasi belum terdeteksi dengan yakin. Masukkan durasi secara manual.")
            activity = st.selectbox("Jenis aktivitas", ["Belajar", "Lari", "Olahraga", "Mengerjakan proyek", "Membaca", "Lainnya"])
            if activity == "Lainnya":
                activity = st.text_input("Nama aktivitas", placeholder="Contoh: latihan coding")
            default_minutes = detected or 60
            colh, colm = st.columns(2)
            with colh:
                hours = st.number_input("Jam", min_value=0, max_value=24, value=default_minutes // 60, step=1)
            with colm:
                minutes = st.number_input("Menit tambahan", min_value=0, max_value=59, value=default_minutes % 60, step=1)
            total_minutes = int(hours * 60 + minutes)
            reward = int((total_minutes / 60) * RATE_PER_HOUR)
            st.metric("Reward jika disetujui", money(reward), help="Dihitung proporsional: Rp5.000 per 60 menit.")
            note = st.text_input("Catatan (opsional)", placeholder="Contoh: belajar materi basis data")
            confirm = st.checkbox("Saya sudah memeriksa bukti dan durasinya benar.")
            if st.button("Konfirmasi & tambahkan reward", type="primary", disabled=(not confirm or total_minutes <= 0 or not str(activity).strip())):
                try:
                    sb.table("transactions").insert({
                        "kind": "activity",
                        "amount": reward,
                        "minutes": total_minutes,
                        "activity": str(activity).strip(),
                        "note": note.strip() or None,
                        "screenshot_hash": digest,
                        "ocr_text": ocr_text[:5000] if ocr_text else None,
                    }).execute()
                    st.success(f"Berhasil! {money(reward)} ditambahkan ke saldo reward.")
                    st.rerun()
                except Exception as exc:
                    msg = str(exc).lower()
                    if "duplicate" in msg or "unique" in msg or "23505" in msg:
                        st.error("Screenshot ini sudah pernah diklaim. Reward tidak ditambahkan lagi.")
                    else:
                        st.error("Reward gagal disimpan. Periksa koneksi database dan struktur tabel.")
                        st.exception(exc)

with tab_spend:
    st.subheader("Tukar reward untuk hobi")
    st.write("Catat pembelian yang benar-benar lo lakukan. Nominal akan mengurangi saldo reward.")
    with st.form("spend_form"):
        hobby = st.text_input("Pengeluaran hobi", placeholder="Contoh: game, gacha, keyboard")
        amount = st.number_input("Nominal (Rp)", min_value=1, max_value=1_000_000_000, value=5000, step=1000)
        spend_note = st.text_input("Catatan (opsional)", placeholder="Contoh: pembelian game diskon")
        submit_spend = st.form_submit_button("Catat pengeluaran", type="primary")
    if submit_spend:
        if not hobby.strip():
            st.error("Isi nama pengeluaran hobi terlebih dahulu.")
        elif amount > balance:
            st.error(f"Saldo tidak cukup. Saldo tersedia: {money(balance)}.")
        else:
            try:
                # Insert is atomic, but concurrent sessions could both pass the balance check.
                # For a single-user personal app this is usually sufficient; see README for hardening.
                sb.table("transactions").insert({
                    "kind": "spend",
                    "amount": -int(amount),
                    "minutes": None,
                    "activity": hobby.strip(),
                    "note": spend_note.strip() or None,
                    "screenshot_hash": None,
                    "ocr_text": None,
                }).execute()
                st.success(f"Pengeluaran {money(amount)} dicatat.")
                st.rerun()
            except Exception as exc:
                st.error("Pengeluaran gagal disimpan.")
                st.exception(exc)

with tab_history:
    st.subheader("Riwayat transaksi")
    if df.empty:
        st.info("Belum ada transaksi. Upload bukti aktivitas untuk mulai mengumpulkan reward.")
    else:
        history = df.copy()
        history["Tanggal"] = history["created_at"].dt.strftime("%d-%m-%Y %H:%M").fillna("—")
        history["Tipe"] = history["kind"].map({"activity": "Reward aktivitas", "spend": "Pengeluaran hobi"}).fillna(history["kind"])
        history["Aktivitas / Hobi"] = history["activity"].fillna("—")
        history["Durasi"] = history["minutes"].apply(lambda v: f"{int(v)//60}j {int(v)%60}m" if pd.notna(v) else "—")
        history["Nominal"] = history["amount"].apply(lambda v: ("+" if int(v) > 0 else "") + money(v))
        history["Catatan"] = history["note"].fillna("—")
        st.dataframe(history[["Tanggal", "Tipe", "Aktivitas / Hobi", "Durasi", "Nominal", "Catatan"]], use_container_width=True, hide_index=True)
        csv = history[["Tanggal", "Tipe", "Aktivitas / Hobi", "Durasi", "Nominal", "Catatan"]].to_csv(index=False).encode("utf-8-sig")
        st.download_button("Unduh riwayat CSV", csv, file_name="hobbyfund-riwayat.csv", mime="text/csv")

with tab_settings:
    st.subheader("Aturan HobbyFund")
    st.markdown(f"""
- **Tarif:** Rp5.000 per 60 menit produktif.
- **Perhitungan:** proporsional; 30 menit = Rp2.500.
- **Konfirmasi manual:** OCR hanya membantu membaca durasi. Reward baru masuk setelah lo memeriksa dan menyetujui.
- **Anti-duplikat:** hash SHA-256 gambar yang sama hanya bisa diklaim sekali.
- **Privasi:** gambar diproses saat upload dan tidak disimpan sebagai file di database. Hash dan teks OCR disimpan sebagai catatan verifikasi.
- **Saldo:** reward positif menambah saldo; transaksi hobi negatif menguranginya.
- **Penting:** ini sistem pencatatan pribadi, bukan verifikasi anti-kecurangan yang sempurna. Screenshot yang diedit atau dipotong dapat lolos dari deteksi duplikat.
""")
    st.caption("Untuk pemakaian pribadi, jangan bagikan URL database key atau file Secrets. Gunakan Supabase anon/publishable key dengan Row Level Security yang sesuai.")
