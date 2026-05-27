# Vehicle Detection and Counting Demo

**Research project:** Vehicle Detection and Monitoring in Traffic Surveillance Video Using Deep Learning.

A full-stack demo system for **object detection and multi-object monitoring** in traffic surveillance using **YOLOv8**. It supports loading a trained model, processing RTSP streams (or video files), drawing ROI regions, and real-time vehicle counting via a web dashboard.

---

## Project objective

- **Vehicle detection** using YOLOv8 (car, bus, truck, motorbike, bicycle, person)
- **Vehicle counting** (line-crossing in ROI)
- **ROI region filtering** (polygon drawn on the video)
- **RTSP camera / video file / YouTube link** processing
- **Real-time visualization** over WebSocket

The architecture is extendable for **SORT**, **DeepSORT**, **ByteTrack**, and **multi-camera** in the future.

**Sơ đồ kiến trúc hệ thống (layout ảnh mẫu, cập nhật theo code hiện tại):** [docs/KIEN-TRUC-HE-THONG.md](docs/KIEN-TRUC-HE-THONG.md).

---

## Tech stack

| Layer           | Technologies                                      |
|----------------|---------------------------------------------------|
| **Backend**    | Python, FastAPI, Ultralytics YOLOv8, OpenCV, Pydantic |
| **Frontend**   | React, TypeScript, Vite, TailwindCSS, Canvas overlay |
| **Infrastructure** | Docker, Docker Compose, `.env` configuration   |

---

## Project structure

```
Web/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI entry
│   │   ├── api/
│   │   │   ├── detection_routes.py   # ROI, stats, settings
│   │   │   ├── model_routes.py       # Upload, list, load, delete models
│   │   │   └── stream_routes.py      # Start/stop stream, WebSocket
│   │   ├── services/
│   │   │   ├── detection_service.py
│   │   │   ├── stream_service.py
│   │   │   ├── model_service.py
│   │   │   └── roi_service.py
│   │   ├── ml/
│   │   │   ├── yolo_model.py         # YOLOv8 wrapper
│   │   │   ├── roi_filter.py
│   │   │   ├── vehicle_counter.py
│   │   │   └── tracker.py            # SORT / DeepSORT trackers
│   │   ├── core/
│   │   │   ├── config.py
│   │   │   └── logger.py
│   │   ├── models/
│   │   │   └── detection_model.py    # Pydantic schemas
│   │   └── utils/
│   │       ├── image_utils.py
│   │       └── video_utils.py
│   ├── models_storage/               # Uploaded YOLO .pt files
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env.example
├── frontend/
│   ├── src/
│   │   ├── components/    # VideoPlayer, RoiDrawer, ModelUploader, CounterPanel
│   │   ├── pages/         # Dashboard
│   │   ├── services/      # api.ts
│   │   ├── hooks/         # useDetection.ts
│   │   ├── types/         # detection.ts
│   │   └── utils/         # canvas.ts
│   ├── package.json
│   └── vite.config.ts
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## Quick start

### Option 1: Docker Compose (recommended)

```bash
# From Web/
docker-compose up --build
```

- **Backend API:** http://localhost:8000  
- **API docs:** http://localhost:8000/docs  
- **Frontend:** http://localhost:5173 (or port in docker-compose for frontend)

If the frontend is built and served by Docker on port 5173, open http://localhost:5173. Adjust `docker-compose.yml` if the frontend is served on a different port.

### Option 2: Local development

**Backend**

```bash
cd backend
python -m venv venv
# Windows: venv\Scripts\activate
# Linux/Mac: source venv/bin/activate
pip install -r requirements.txt
# Copy .env from .env.example if needed
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Frontend**

```bash
cd frontend
npm install
npm run dev
```

- Frontend: http://localhost:5173 (proxies `/api` to backend:8000)
- Backend: http://localhost:8000

**Chạy bằng file .bat (Windows)**  
Trong PowerShell hoặc CMD, từ thư mục `Web` chạy **file** (không dùng `cd` vào file):

```powershell
.\run_backend.bat    # Terminal 1 – backend
.\run_frontend.bat   # Terminal 2 – frontend
```

---

## Configuration

- **Backend:** copy `backend/.env.example` to `backend/.env` and set:
  - `PORT`, `DEBUG`, `CONF_THRESHOLD`, `LINE_POSITION`, `MAX_FPS`
- **Models:** uploaded `.pt` files are stored in `backend/models_storage/`.
- **CORS:** defaults allow `localhost:5173`, `3000`, `5000`; override via env if needed.

---

## Testing RTSP stream with a video file

You can simulate an RTSP source from a local video using **ffmpeg**:

```bash
# Stream a video file as RTSP (loop forever)
ffmpeg -re -stream_loop -1 -i traffic.mp4 -c copy -f rtsp rtsp://localhost:8554/traffic
```

Requires an RTSP server listening on `localhost:8554` (e.g. [mediamtx](https://github.com/bluenviron/mediamtx)). Then in the dashboard set the stream URL to:

```
rtsp://localhost:8554/traffic
```

**Alternative:** use a **local video path** in the stream URL (e.g. `C:/path/to/traffic.mp4` or `/path/to/traffic.mp4`). The backend uses OpenCV and can open local files directly.

**YouTube:** you can paste a **YouTube link** directly (e.g. `https://www.youtube.com/watch?v=...` or `https://youtu.be/...`). The backend uses **yt-dlp** to resolve it to a direct stream URL. Install with `pip install yt-dlp` (already in `requirements.txt`). Some videos may not work (region, format, or age restrictions).

---

## API overview

| Method   | Endpoint              | Description                    |
|----------|------------------------|--------------------------------|
| GET      | `/health`              | Health check                   |
| GET      | `/api/v1/models`       | List available models          |
| POST     | `/api/v1/models/upload`| Upload YOLO .pt (stored in `models_storage`) |
| POST     | `/api/v1/models/load`  | Load model by name             |
| DELETE   | `/api/v1/models/{name}`| Delete model file              |
| POST     | `/api/v1/stream/start` | Start capture (body: `{ "url": "rtsp://... or YouTube or file path" }`) |
| POST     | `/api/v1/stream/stop`  | Stop stream                    |
| GET      | `/api/v1/stream/status`| Stream active, FPS, frame count|
| WebSocket| `/api/v1/stream/ws`    | Real-time frames + detections + stats |
| POST     | `/api/v1/roi`          | Set ROI (body: `{ "points": [[x,y],...], "active": true }`) |
| DELETE   | `/api/v1/roi`          | Clear ROI                      |
| GET      | `/api/v1/stats`        | Vehicle counts, FPS, model, ROI |
| POST     | `/api/v1/stats/reset`  | Reset counters                 |
| GET      | `/api/v1/settings`     | conf_threshold, line_position, max_fps |
| PATCH    | `/api/v1/settings`    | Update settings                |

---

## Dashboard layout

- **Top:** Model upload, RTSP/video URL input, Start/Stop detection.
- **Center:** Live video with bounding boxes and ROI overlay; ROI drawing tool.
- **Right:** Vehicle statistics (counts by class), reset, export CSV.

UI is minimal and professional: light background, neutral colors (white, gray, blue).

---

## Detection pipeline

```
RTSP / Video → OpenCV capture → YOLOv8 inference → ROI filter → SORT/DeepSORT
→ Vehicle counting (line cross) → WebSocket payload (frame + detections + stats)
→ Frontend canvas overlay
```

AI logic lives in the `ml/` module; API and services stay free of direct ML code for clean separation and future tracker swaps (SORT, DeepSORT, ByteTrack).

---

## License & purpose

This project is for **academic research** and demonstration. Use in production at your own risk; ensure compliance with data and privacy regulations when processing real camera streams.
