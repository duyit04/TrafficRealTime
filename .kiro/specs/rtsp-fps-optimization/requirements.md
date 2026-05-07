# Requirements Document

## Introduction

Hệ thống hiện tại hiển thị stream RTSP với FPS thấp do kiến trúc polling HTTP (tối đa 4 FPS hiển thị), YOLO inference nặng trên mỗi frame, và pipeline xử lý đồng bộ. Tính năng này tối ưu toàn bộ pipeline để đạt FPS hiển thị cao hơn và độ trễ thấp hơn khi xem RTSP trực tiếp.

## Glossary

- **StreamService**: Service backend quản lý vòng lặp đọc frame từ RTSP và chạy pipeline detection.
- **Worker**: Thread nền trong StreamService thực hiện đọc frame và inference.
- **Pipeline**: Chuỗi xử lý mỗi frame: đọc RTSP → YOLO track → đếm xe → đóng gói payload.
- **Polling**: Cơ chế frontend gọi HTTP GET định kỳ để lấy frame mới nhất.
- **WebSocket**: Giao thức kết nối hai chiều liên tục, loại bỏ overhead của HTTP polling.
- **Frame Interval**: Khoảng thời gian tối thiểu giữa hai lần xử lý frame (= 1 / max_fps).
- **JPEG Quality**: Tham số nén ảnh (0–100); giá trị thấp hơn → file nhỏ hơn → truyền nhanh hơn.
- **imgsz**: Kích thước ảnh đầu vào cho YOLO inference; nhỏ hơn → nhanh hơn nhưng kém chính xác hơn.
- **Skip Frame**: Bỏ qua inference trên một số frame để tăng FPS hiển thị.
- **FPS hiển thị**: Số frame frontend nhận và render được mỗi giây.
- **FPS xử lý**: Số frame backend chạy inference được mỗi giây.

## Requirements

### Requirement 1: WebSocket thay thế HTTP polling

**User Story:** As a người dùng, I want xem stream RTSP mượt mà với FPS cao, so that tôi có thể quan sát giao thông theo thời gian thực.

#### Acceptance Criteria

1. WHEN stream RTSP đang hoạt động, THE StreamService SHALL phát frame mới nhất qua WebSocket endpoint `/ws/stream` ngay khi có frame mới.
2. WHEN frontend kết nối WebSocket, THE System SHALL gửi frame liên tục mà không cần client request từng frame.
3. WHEN WebSocket bị ngắt kết nối, THE System SHALL cho phép client reconnect và tiếp tục nhận frame.
4. IF WebSocket không khả dụng, THEN THE Frontend SHALL fallback về HTTP polling với interval 250ms.
5. THE WebSocket endpoint SHALL gửi payload JSON gồm `frame` (base64 JPEG), `detections`, và `stats`.

### Requirement 2: Giảm kích thước frame truyền đi

**User Story:** As a người dùng, I want nhận frame nhanh hơn, so that độ trễ hiển thị thấp hơn.

#### Acceptance Criteria

1. THE StreamService SHALL encode frame với JPEG quality mặc định là 75 (thay vì 92).
2. WHERE cấu hình `STREAM_JPEG_QUALITY` được đặt trong `.env`, THE StreamService SHALL sử dụng giá trị đó thay vì mặc định.
3. THE StreamService SHALL hỗ trợ cấu hình `STREAM_MAX_WIDTH` để resize frame trước khi encode (mặc định 0 = không resize).
4. WHEN `STREAM_MAX_WIDTH` > 0 và frame rộng hơn giá trị đó, THE StreamService SHALL resize frame xuống `STREAM_MAX_WIDTH` pixel chiều rộng trước khi encode JPEG.

### Requirement 3: Skip frame inference để tăng FPS hiển thị

**User Story:** As a người dùng, I want thấy video mượt hơn ngay cả khi inference chậm, so that trải nghiệm xem không bị giật.

#### Acceptance Criteria

1. THE StreamService SHALL hỗ trợ cấu hình `INFERENCE_SKIP_FRAMES` (mặc định 0 = không skip).
2. WHEN `INFERENCE_SKIP_FRAMES` = N > 0, THE StreamService SHALL chỉ chạy YOLO inference trên 1 trong N+1 frame liên tiếp.
3. WHEN frame bị skip inference, THE StreamService SHALL sử dụng lại kết quả detections từ frame inference gần nhất.
4. WHEN frame bị skip inference, THE StreamService SHALL vẫn encode và gửi frame đó để hiển thị mượt.
5. THE StreamService SHALL tính `fps` dựa trên số frame được gửi đi (bao gồm cả frame skip), không chỉ frame inference.

### Requirement 4: Cấu hình YOLO inference size

**User Story:** As a người quản trị, I want điều chỉnh kích thước inference của YOLO, so that tôi có thể đánh đổi giữa tốc độ và độ chính xác.

#### Acceptance Criteria

1. THE YOLOModel SHALL đọc cấu hình `YOLO_IMGSZ` từ settings (mặc định 640).
2. WHEN `YOLO_IMGSZ` được đặt thành 320 hoặc 416, THE YOLOModel SHALL sử dụng kích thước đó cho cả `predict()` và `track()`.
3. THE Settings SHALL validate `YOLO_IMGSZ` chỉ chấp nhận các giá trị: 320, 416, 480, 640.
4. IF `YOLO_IMGSZ` không hợp lệ, THEN THE Settings SHALL sử dụng giá trị mặc định 640 và ghi log cảnh báo.

### Requirement 5: API cập nhật cấu hình stream runtime

**User Story:** As a người dùng, I want thay đổi cấu hình FPS và chất lượng stream mà không cần restart, so that tôi có thể điều chỉnh theo điều kiện mạng.

#### Acceptance Criteria

1. THE Settings API (`PATCH /api/v1/settings`) SHALL chấp nhận thêm các trường `jpeg_quality` (int, 30–95) và `max_width` (int, 0 hoặc 320–1920).
2. WHEN `jpeg_quality` được cập nhật, THE StreamService SHALL áp dụng ngay cho frame tiếp theo mà không cần restart stream.
3. WHEN `max_width` được cập nhật, THE StreamService SHALL áp dụng ngay cho frame tiếp theo.
4. THE Settings GET endpoint SHALL trả về các giá trị `jpeg_quality` và `max_width` hiện tại.


### Requirement 6: Tối ưu RTSP buffer flushing

**User Story:** As a người dùng, I want xem frame mới nhất từ camera, so that không bị trễ do buffer tích lũy.

#### Acceptance Criteria

1. WHEN đọc frame từ RTSP, THE StreamService SHALL gọi `cap.grab()` nhiều lần để flush buffer cũ trước khi `retrieve()` frame cuối.
2. THE StreamService SHALL đọc cấu hình `RTSP_FLUSH_FRAMES` (mặc định 2) để xác định số lần grab.
3. WHEN inference chậm hơn camera FPS, THE StreamService SHALL tăng số lần flush để đảm bảo frame luôn mới nhất.
4. THE StreamService SHALL ghi log cảnh báo khi phát hiện pipeline chậm hơn camera FPS (sleep_t < 0 liên tục).

### Requirement 7: Tách pipeline nặng ra thread riêng

**User Story:** As a người phát triển, I want pipeline nặng (traffic light logic, behavior extractor) không chặn việc gửi frame, so that FPS hiển thị không bị giảm.

#### Acceptance Criteria

1. THE StreamService SHALL tách logic traffic light controller và behavior extractor ra một thread riêng `_analytics_worker`.
2. WHEN frame mới được xử lý, THE StreamService SHALL đẩy detections và tracks vào queue để `_analytics_worker` xử lý bất đồng bộ.
3. THE StreamService SHALL gửi frame + detections qua WebSocket ngay sau YOLO inference, không đợi analytics hoàn thành.
4. WHEN analytics queue đầy (> 10 items), THE StreamService SHALL bỏ qua frame cũ để tránh tích lũy.
5. THE `_analytics_worker` SHALL cập nhật traffic light state và congestion state vào shared state thread-safe.

### Requirement 8: Monitoring và metrics FPS

**User Story:** As a người quản trị, I want theo dõi FPS thực tế của từng giai đoạn pipeline, so that tôi có thể xác định bottleneck.

#### Acceptance Criteria

1. THE StreamService SHALL tính và lưu các metrics: `fps_capture` (đọc frame), `fps_inference` (YOLO), `fps_sent` (gửi qua WebSocket).
2. THE Stats API (`GET /api/v1/stats`) SHALL trả về cả ba giá trị FPS trên.
3. WHEN pipeline bị chậm, THE StreamService SHALL ghi log chi tiết thời gian từng bước: capture, inference, encode, send.
4. THE StreamService SHALL tính `avg_inference_ms` (trung bình thời gian inference 10 frame gần nhất) và expose qua stats API.

### Requirement 9: Frontend WebSocket client

**User Story:** As a người dùng, I want frontend tự động kết nối WebSocket khi stream bắt đầu, so that tôi nhận frame nhanh nhất có thể.

#### Acceptance Criteria

1. WHEN stream được start, THE Frontend SHALL mở WebSocket connection tới `/ws/stream`.
2. WHEN nhận message từ WebSocket, THE Frontend SHALL parse JSON và cập nhật `currentFrame`, `detections`, `stats`.
3. IF WebSocket bị lỗi hoặc đóng, THE Frontend SHALL thử reconnect sau 2 giây (tối đa 5 lần).
4. WHEN stream bị stop, THE Frontend SHALL đóng WebSocket connection.
5. THE Frontend SHALL hiển thị trạng thái kết nối WebSocket (connected/disconnected) trong UI.

### Requirement 10: Backward compatibility với HTTP polling

**User Story:** As a người dùng, I want hệ thống vẫn hoạt động nếu WebSocket không khả dụng, so that tôi không bị gián đoạn dịch vụ.

#### Acceptance Criteria

1. THE Frontend SHALL kiểm tra WebSocket support trước khi kết nối.
2. IF WebSocket không được hỗ trợ hoặc kết nối thất bại sau 5 lần thử, THEN THE Frontend SHALL fallback về HTTP polling với interval 250ms.
3. THE Backend SHALL vẫn hỗ trợ endpoint `GET /api/v1/stream/frame` cho HTTP polling.
4. WHEN sử dụng HTTP polling, THE Frontend SHALL hiển thị cảnh báo "Đang dùng chế độ tương thích (FPS thấp hơn)".
