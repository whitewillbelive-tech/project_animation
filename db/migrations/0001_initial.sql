-- AI 애니메이션: 현재 app/store.py의 초기 스키마와 동일한 정의.
-- 스키마만 포함한다. 계정, 암호 해시, 세션, 프로젝트, 사용자 자료는 없다.
-- 실행 중인 앱은 Store 초기화에서 같은 CREATE ... IF NOT EXISTS를 실행한다.
-- 이 파일은 기존 DB의 열 변경, 초기화, 데이터 이관을 수행하지 않는다.
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL,csrf TEXT NOT NULL,project_id TEXT,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stages(id TEXT NOT NULL,project_id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(id,project_id));
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS records_project ON records(project_id);
CREATE INDEX IF NOT EXISTS events_project ON events(project_id);
