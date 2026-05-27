# Traffic Monitor (YOLOv8 + FastAPI + React)

Hệ thống giám sát giao thông realtime dùng YOLOv8 để phát hiện và theo dõi phương tiện từ RTSP/video, hiển thị trên dashboard web, kèm ROI, thống kê và module traffic-light.

## Tính năng chính

- Nhận nguồn vào từ `RTSP`, file video local, hoặc link YouTube.
- Phát hiện đối tượng bằng YOLOv8, hỗ trợ model `.pt` và `.engine`.
- Theo dõi/đếm phương tiện theo ROI và line-crossing.
- Stream realtime về frontend qua WebSocket.
- Quản lý model trực tiếp qua API (upload, load, delete, export TensorRT).
- Điều khiển và lấy trạng thái traffic-light qua API riêng.
- Hỗ trợ xử lý ảnh/video tĩnh qua nhóm API media.

## Công nghệ sử dụng

- Backend: Python, FastAPI, OpenCV, Ultralytics YOLO, Pydantic
- Frontend: React, TypeScript, Vite
- Runtime: Docker Compose hoặc chạy local (Windows/Linux)

## Cấu trúc thư mục

```text
webtraffic/
|- backend/
|  |- app/
|  |  |- api/                 # model/stream/detection/traffic-light/media routes
|  |  |- services/            # stream, detection, model, traffic-light...
|  |  |- ml/                  # YOLO wrapper, tracker, counter
|  |  `- main.py              # FastAPI entrypoint
|  |- models_storage/         # Model upload/runtime storage
|  `- requirements.txt
|- frontend/
|  |- src/
|  `- package.json
|- docker-compose.yml
|- run.bat                    # One-click startup (Windows)
`- README.md
```

## Chạy nhanh

### Cách 1: One-click trên Windows (khuyến nghị)

Từ thư mục gốc project:

```powershell
.\run.bat
```

Script sẽ tự:
- kiểm tra Python/npm,
- tạo `venv-gpu` nếu chưa có,
- cài dependencies backend/frontend nếu thiếu,
- khởi động backend `:8000` và frontend `:5173`.

Sau khi chạy:
- Frontend: `http://localhost:5173`
- Backend docs: `http://localhost:8000/docs`

### Cách 2: Docker Compose

```bash
docker-compose up --build
```

### Cách 3: Local thủ công

Backend:

```bash
cd backend
python -m venv venv
# Windows: venv\Scripts\activate
# Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Frontend:

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 5173
```

## Cấu hình cơ bản

- Copy `backend/.env.example` -> `backend/.env` rồi chỉnh thông số cần thiết.
- Root `.env.example` dùng cho cấu hình tổng thể khi cần.
- Model đặt trong `backend/models_storage/` hoặc upload qua API.

## Nhóm API chính

- `GET /health` - kiểm tra trạng thái service.
- `POST /api/v1/stream/start` - chạy luồng chính.
- `POST /api/v1/stream/companion/start` - chạy luồng companion.
- `POST /api/v1/stream/extra/start` - chạy luồng extra.
- `GET /api/v1/stream/status` - trạng thái runtime stream.
- `GET /api/v1/models` - danh sách model.
- `POST /api/v1/models/upload` - upload model.
- `POST /api/v1/models/export-engine` - export TensorRT engine.
- `POST /api/v1/roi` - cập nhật ROI.
- `GET /api/v1/stats` - thống kê detection/count.
- `GET /api/v1/traffic-light/state` - trạng thái traffic-light.
- `POST /api/v1/media/detect-image` - detect trên ảnh.

Danh sách endpoint đầy đủ xem trực tiếp tại Swagger: `http://localhost:8000/docs`.

## Ghi chú triển khai

- Trên Windows + NVIDIA GPU, nên dùng `run.bat` để tự cài đúng gói CUDA/TensorRT theo cấu hình project.
- Khi dùng camera RTSP, hệ thống hiện ưu tiên transport TCP để ổn định.
- Nếu không có model custom, backend sẽ tự fallback model mặc định khi khởi động.

## Tài liệu liên quan

- Kiến trúc hệ thống: `docs/KIEN-TRUC-HE-THONG.md`
- Ghi chú vận hành và thay đổi gần đây: `.ai-memory.md`

## Mục đích sử dụng

Dự án phục vụ nghiên cứu và demo. Khi triển khai thực tế cần tự đánh giá bảo mật, hiệu năng, quyền riêng tư và tuân thủ pháp lý dữ liệu camera.
