-- 추천 판정에 따른 사람별 관심 분야(JSON 배열, 예: ["toddler","senior"]). 고른 분야는 '관심 설정 시'·'일반 제외' 행사도 받는다.
ALTER TABLE people ADD COLUMN interests TEXT;
