# Implementation Plan: RTSP FPS Optimization

## Overview

Tối ưu pipeline RTSP để tăng FPS hiển thị từ ~4 FPS (HTTP polling) lên 15–25 FPS bằng cách: thêm WebSocket broadcast, tách analytics thread, skip frame inference, giảm JPEG quality, và thêm metrics.

## Tasks

- [x] 1. Mở rộng Settings và Data Models
  - Thêm `STREAM_JPEG_QUALITY`, `STREAM_MAX_WIDTH`, `INFERENCE_SKIP_FRAMES`, `YOLO_IMGSZ`, `RTSP_FLUSH_FRAMES` vào `backend/app/core/config.py`
  - Thêm `jpeg_quality`, `max_width`, `skip_frames` vào `SettingsUpdate` trong `backend/app/models/detection_model.py`
  - Thêm `fps_capture`, `fps_inference`, `fps_sent`, `avg_inference_ms` vào `VehicleStats`
  - Cập nhật `YOLO_IMGSZ` validation: chỉ chấp nhận {320, 416, 480, 640}, fallback 640 nếu invalid
  - _Requirements: 2.2, 4.1, 4.3, 4.4, 5.1, 8.1_

- [ ]* 1.1 Viết unit test cho YOLO_IMGSZ validation
  - Test giá trị hợp lệ: 320, 416, 480, 640
  - Test giá trị invalid: 100, 512, 1280 → phải fallback về 640
  - _Requirements: 4.3, 4.4_

- [ ]* 1.2 Viết property test cho YOLO_IMGSZ validation
  - **Property 5: YOLO_IMGSZ validation**
  - **Validates: Requirements 4.3, 4.4**
  - Generate random int values, kiểm tra output luôn thuộc {320, 416, 480, 640}

- [x] 2. Cập nhật YOLOModel để dùng YOLO_IMGSZ từ settings
  - Sửa `predict()` và `track()` trong `backend/app/ml/yolo_model.py` để dùng `settings.YOLO_IMGSZ` thay vì hardcode 640
  - _Requirements: 4.1, 4.2_

- [x] 3. Thêm WebSocket ConnectionManager và endpoint
  - Tạo file `backend/app/api/ws_routes.py`
  - Implement `ConnectionManager` với `connect()`, `disconnect()`, `broadcast()`
  - Thêm WebSocket route `GET /ws/stream`
  - Đăng ký router trong `backend/app/main.py`
  - _Requirements: 1.1, 1.2, 1.3, 1.5_

- [ ]* 3.1 Viết unit test cho ConnectionManager
  - Test connect/disconnect quản lý danh sách đúng
  - Test broadcast tới nhiều clients
  - Test broadcast khi có client lỗi không ảnh hưởng client khác
  - _Requirements: 1.3_

- [ ]* 3.2 Viết property test cho WebSocket payload schema
  - **Property 1: WebSocket payload luôn có đủ trường**
  - **Validates: Requirements 1.2, 1.5**
  - Generate random frame + detections data, kiểm tra JSON schema đầu ra

- [x] 4. Tách Analytics Worker thread trong StreamService
  - Thêm `_analytics_queue: queue.Queue(maxsize=10)` vào `StreamService.__init__`
  - Tạo method `_analytics_worker()` xử lý: ROI counting, VehicleCounter, CongestionMonitor, TrafficLight, BehaviorExtractor
  - Sửa `_worker()`: sau YOLO inference, push `(tracks, raw_dets, frame_meta)` vào queue (non-blocking, drop nếu đầy)
  - Start `_analytics_thread` khi stream bắt đầu, stop khi stream dừng
  - _Requirements: 7.1, 7.2, 7.4, 7.5_

- [ ]* 4.1 Viết property test cho analytics queue drop
  - **Property 7: Analytics queue không block frame gửi**
  - **Validates: Requirements 7.2, 7.4**
  - Simulate analytics chậm (sleep), kiểm tra queue drop không block worker

- [x] 5. Implement skip frame inference và frame resize trong Worker
  - Thêm `skip_frames`, `_skip_counter`, `_last_raw_dets` vào `StreamService`
  - Sửa `_worker()`: chỉ gọi `yolo_model.track()` khi `_skip_counter % (skip_frames+1) == 0`
  - Implement resize: nếu `max_width > 0` và frame rộng hơn, dùng `cv2.resize()` trước encode
  - Đổi JPEG quality từ 92 sang `self.jpeg_quality` (default từ settings)
  - Cập nhật `fps_capture`, `fps_inference`, `fps_sent`, `avg_inference_ms` metrics
  - _Requirements: 2.1, 2.4, 3.1, 3.2, 3.3, 3.4, 3.5, 8.1, 8.4_

- [ ]* 5.1 Viết property test cho skip frame reuse
  - **Property 3: Skip frame reuse detections**
  - **Validates: Requirements 3.2, 3.3, 3.4**
  - Generate random skip_frames values, mock YOLO, đếm số lần gọi và kiểm tra reuse

- [ ]* 5.2 Viết property test cho frame resize
  - **Property 2: Frame resize không vượt max_width**
  - **Validates: Requirements 2.3, 2.4**
  - Generate random frame sizes và max_width values, kiểm tra output width <= max_width

- [ ]* 5.3 Viết property test cho FPS sent
  - **Property 4: FPS sent phản ánh số frame gửi**
  - **Validates: Requirements 3.5, 8.1**
  - Simulate frame loop, kiểm tra fps_sent xấp xỉ số frame / thời gian

- [ ]* 5.4 Viết property test cho avg_inference_ms sliding window
  - **Property 9: avg_inference_ms là sliding window 10 frame**
  - **Validates: Requirements 8.4**
  - Generate random inference time sequences, kiểm tra sliding window average

- [x] 6. Kết nối StreamService với WebSocket broadcaster
  - Sửa `_worker()`: sau khi encode frame, gọi `asyncio` để broadcast payload qua `ConnectionManager`
  - Dùng `asyncio.run_coroutine_threadsafe()` để gọi async broadcast từ sync thread
  - Lưu reference tới event loop trong `StreamService` khi start
  - _Requirements: 1.1, 1.2_

- [x] 7. Cập nhật Settings API để expose và nhận cấu hình mới
  - Sửa `GET /api/v1/settings` trong `detection_routes.py` để trả về `jpeg_quality`, `max_width`, `skip_frames`
  - Sửa `PATCH /api/v1/settings` để nhận và áp dụng `jpeg_quality`, `max_width`, `skip_frames`
  - Sửa `StreamService.update_settings()` để nhận các tham số mới
  - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [ ]* 7.1 Viết property test cho settings update áp dụng ngay
  - **Property 6: Settings update áp dụng ngay**
  - **Validates: Requirements 5.2**
  - Patch jpeg_quality, kiểm tra giá trị được lưu trong StreamService ngay lập tức

- [ ] 8. Checkpoint — Đảm bảo tất cả tests backend pass
  - Chạy `pytest backend/` và đảm bảo tất cả tests pass
  - Kiểm tra WebSocket endpoint hoạt động với `wscat` hoặc test client
  - Hỏi user nếu có vấn đề

- [x] 9. Tạo useWebSocket hook ở Frontend
  - Tạo file `frontend/src/hooks/useWebSocket.ts`
  - Implement WebSocket connection, `onmessage` handler, reconnect logic (tối đa 5 lần, delay 2s)
  - Sau 5 lần thất bại: set `usingFallback = true`
  - Expose `connected`, `usingFallback`, `disconnect`
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 10.1, 10.2_

- [ ]* 9.1 Viết property test cho WebSocket reconnect retry
  - **Property 8: WebSocket reconnect retry limit**
  - **Validates: Requirements 9.3, 10.2**
  - Mock WebSocket failures, kiểm tra retry count <= 5 và fallback trigger

- [x] 10. Thêm WebSocket URL helper vào api.ts và cập nhật types
  - Thêm `getWebSocketUrl()` vào `frontend/src/services/api.ts`
  - Thêm `fps_capture`, `fps_inference`, `fps_sent`, `avg_inference_ms` vào `VehicleStats` type
  - Thêm `jpeg_quality`, `max_width`, `skip_frames` vào `Settings` type
  - _Requirements: 5.1, 8.1_

- [x] 11. Cập nhật useDetection hook để dùng WebSocket
  - Sửa `frontend/src/hooks/useDetection.ts`: thay `setInterval(250ms)` bằng `useWebSocket`
  - Khi `usingFallback = true`: giữ nguyên HTTP polling logic
  - Expose `wsConnected` và `usingFallback` từ hook
  - _Requirements: 1.4, 9.1, 9.4, 9.5, 10.3, 10.4_

- [x] 12. Hiển thị trạng thái WebSocket trong UI
  - Tìm component hiển thị stream status (stream_routes hoặc UI component)
  - Thêm indicator: "WebSocket" (xanh) hoặc "Polling fallback" (vàng) dựa trên `usingFallback`
  - _Requirements: 9.5, 10.4_

- [x] 13. Checkpoint cuối — Đảm bảo tất cả tests pass
  - Chạy `pytest backend/` và `npm run test` trong `frontend/`
  - Kiểm tra stream RTSP thực tế: FPS hiển thị phải cao hơn trước
  - Hỏi user nếu có vấn đề

## Notes

- Tasks đánh dấu `*` là optional (tests), có thể bỏ qua để MVP nhanh hơn
- Mỗi task references requirements cụ thể để traceability
- Checkpoint ở task 8 và 13 để validate từng giai đoạn
- Property tests dùng `pytest-hypothesis` (backend) và `vitest` (frontend)
