-- 지인 공유 마무리: 기준 도시에서 받을 거리(마일). NULL·0 이면 거리로 거르지 않는다.
-- 수신자 상태에 'stopped'(본인이 /stop 으로 중단)가 추가된다(열 형식은 그대로 TEXT).
ALTER TABLE people ADD COLUMN radius_mi INTEGER;
