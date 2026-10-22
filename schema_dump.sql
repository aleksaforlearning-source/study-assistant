--
-- PostgreSQL database dump
--

\restrict 50N0FpKGG9SGAuddjr1Hxm5bh9XhnqEeavoHGmO6RfA59iEAf3Nt2Qyvalr81kE

-- Dumped from database version 16.15 (Debian 16.15-1.pgdg12+2)
-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg12+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: blocks; Type: TABLE; Schema: public; Owner: studyuser
--

CREATE TABLE public.blocks (
    id integer NOT NULL,
    subject_id integer NOT NULL,
    name text NOT NULL,
    "position" integer DEFAULT 0 NOT NULL
);


ALTER TABLE public.blocks OWNER TO studyuser;

--
-- Name: blocks_id_seq; Type: SEQUENCE; Schema: public; Owner: studyuser
--

CREATE SEQUENCE public.blocks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.blocks_id_seq OWNER TO studyuser;

--
-- Name: blocks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: studyuser
--

ALTER SEQUENCE public.blocks_id_seq OWNED BY public.blocks.id;


--
-- Name: chunks; Type: TABLE; Schema: public; Owner: studyuser
--

CREATE TABLE public.chunks (
    id integer NOT NULL,
    source_id integer NOT NULL,
    text text NOT NULL,
    page_number integer,
    is_weak boolean DEFAULT false,
    drawings_count integer DEFAULT 0,
    embedding public.vector(384)
);


ALTER TABLE public.chunks OWNER TO studyuser;

--
-- Name: chunks_id_seq; Type: SEQUENCE; Schema: public; Owner: studyuser
--

CREATE SEQUENCE public.chunks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.chunks_id_seq OWNER TO studyuser;

--
-- Name: chunks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: studyuser
--

ALTER SEQUENCE public.chunks_id_seq OWNED BY public.chunks.id;


--
-- Name: exam_questions; Type: TABLE; Schema: public; Owner: studyuser
--

CREATE TABLE public.exam_questions (
    id integer NOT NULL,
    topic_id integer NOT NULL,
    source_id integer NOT NULL,
    question_text text NOT NULL
);


ALTER TABLE public.exam_questions OWNER TO studyuser;

--
-- Name: exam_questions_id_seq; Type: SEQUENCE; Schema: public; Owner: studyuser
--

CREATE SEQUENCE public.exam_questions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.exam_questions_id_seq OWNER TO studyuser;

--
-- Name: exam_questions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: studyuser
--

ALTER SEQUENCE public.exam_questions_id_seq OWNED BY public.exam_questions.id;


--
-- Name: jobs; Type: TABLE; Schema: public; Owner: studyuser
--

CREATE TABLE public.jobs (
    id integer NOT NULL,
    source_id integer NOT NULL,
    stage text DEFAULT 'queued'::text NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    message text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.jobs OWNER TO studyuser;

--
-- Name: jobs_id_seq; Type: SEQUENCE; Schema: public; Owner: studyuser
--

CREATE SEQUENCE public.jobs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.jobs_id_seq OWNER TO studyuser;

--
-- Name: jobs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: studyuser
--

ALTER SEQUENCE public.jobs_id_seq OWNED BY public.jobs.id;


--
-- Name: notes; Type: TABLE; Schema: public; Owner: studyuser
--

CREATE TABLE public.notes (
    id integer NOT NULL,
    topic_id integer NOT NULL,
    text text NOT NULL,
    kind text DEFAULT 'own_explanation'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.notes OWNER TO studyuser;

--
-- Name: notes_id_seq; Type: SEQUENCE; Schema: public; Owner: studyuser
--

CREATE SEQUENCE public.notes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.notes_id_seq OWNER TO studyuser;

--
-- Name: notes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: studyuser
--

ALTER SEQUENCE public.notes_id_seq OWNED BY public.notes.id;


--
-- Name: sources; Type: TABLE; Schema: public; Owner: studyuser
--

CREATE TABLE public.sources (
    id integer NOT NULL,
    subject_id integer NOT NULL,
    type text NOT NULL,
    filename text,
    uploaded_at timestamp with time zone DEFAULT now(),
    content_hash text,
    block_id integer,
    pdf bytea
);


ALTER TABLE public.sources OWNER TO studyuser;

--
-- Name: sources_id_seq; Type: SEQUENCE; Schema: public; Owner: studyuser
--

CREATE SEQUENCE public.sources_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.sources_id_seq OWNER TO studyuser;

--
-- Name: sources_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: studyuser
--

ALTER SEQUENCE public.sources_id_seq OWNED BY public.sources.id;


--
-- Name: struggles; Type: TABLE; Schema: public; Owner: studyuser
--

CREATE TABLE public.struggles (
    id integer NOT NULL,
    topic_id integer NOT NULL,
    event_type text NOT NULL,
    context text,
    created_at timestamp with time zone DEFAULT now(),
    resolved_at timestamp with time zone
);


ALTER TABLE public.struggles OWNER TO studyuser;

--
-- Name: struggles_id_seq; Type: SEQUENCE; Schema: public; Owner: studyuser
--

CREATE SEQUENCE public.struggles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.struggles_id_seq OWNER TO studyuser;

--
-- Name: struggles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: studyuser
--

ALTER SEQUENCE public.struggles_id_seq OWNED BY public.struggles.id;


--
-- Name: subjects; Type: TABLE; Schema: public; Owner: studyuser
--

CREATE TABLE public.subjects (
    id integer NOT NULL,
    name text NOT NULL,
    exam_date date
);


ALTER TABLE public.subjects OWNER TO studyuser;

--
-- Name: subjects_id_seq; Type: SEQUENCE; Schema: public; Owner: studyuser
--

CREATE SEQUENCE public.subjects_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.subjects_id_seq OWNER TO studyuser;

--
-- Name: subjects_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: studyuser
--

ALTER SEQUENCE public.subjects_id_seq OWNED BY public.subjects.id;


--
-- Name: techniques; Type: TABLE; Schema: public; Owner: studyuser
--

CREATE TABLE public.techniques (
    id integer NOT NULL,
    topic_id integer NOT NULL,
    source_id integer,
    description text NOT NULL,
    origin text DEFAULT 'ai'::text NOT NULL
);


ALTER TABLE public.techniques OWNER TO studyuser;

--
-- Name: techniques_id_seq; Type: SEQUENCE; Schema: public; Owner: studyuser
--

CREATE SEQUENCE public.techniques_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.techniques_id_seq OWNER TO studyuser;

--
-- Name: techniques_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: studyuser
--

ALTER SEQUENCE public.techniques_id_seq OWNED BY public.techniques.id;


--
-- Name: topic_chunks; Type: TABLE; Schema: public; Owner: studyuser
--

CREATE TABLE public.topic_chunks (
    topic_id integer NOT NULL,
    chunk_id integer NOT NULL
);


ALTER TABLE public.topic_chunks OWNER TO studyuser;

--
-- Name: topics; Type: TABLE; Schema: public; Owner: studyuser
--

CREATE TABLE public.topics (
    id integer NOT NULL,
    subject_id integer NOT NULL,
    parent_id integer,
    title text NOT NULL,
    parent_origin text DEFAULT 'ai'::text NOT NULL,
    status text DEFAULT 'new'::text NOT NULL,
    summary text,
    summary_origin text DEFAULT 'ai'::text NOT NULL
);


ALTER TABLE public.topics OWNER TO studyuser;

--
-- Name: topics_id_seq; Type: SEQUENCE; Schema: public; Owner: studyuser
--

CREATE SEQUENCE public.topics_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.topics_id_seq OWNER TO studyuser;

--
-- Name: topics_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: studyuser
--

ALTER SEQUENCE public.topics_id_seq OWNED BY public.topics.id;


--
-- Name: blocks id; Type: DEFAULT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.blocks ALTER COLUMN id SET DEFAULT nextval('public.blocks_id_seq'::regclass);


--
-- Name: chunks id; Type: DEFAULT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.chunks ALTER COLUMN id SET DEFAULT nextval('public.chunks_id_seq'::regclass);


--
-- Name: exam_questions id; Type: DEFAULT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.exam_questions ALTER COLUMN id SET DEFAULT nextval('public.exam_questions_id_seq'::regclass);


--
-- Name: jobs id; Type: DEFAULT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.jobs ALTER COLUMN id SET DEFAULT nextval('public.jobs_id_seq'::regclass);


--
-- Name: notes id; Type: DEFAULT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.notes ALTER COLUMN id SET DEFAULT nextval('public.notes_id_seq'::regclass);


--
-- Name: sources id; Type: DEFAULT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.sources ALTER COLUMN id SET DEFAULT nextval('public.sources_id_seq'::regclass);


--
-- Name: struggles id; Type: DEFAULT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.struggles ALTER COLUMN id SET DEFAULT nextval('public.struggles_id_seq'::regclass);


--
-- Name: subjects id; Type: DEFAULT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.subjects ALTER COLUMN id SET DEFAULT nextval('public.subjects_id_seq'::regclass);


--
-- Name: techniques id; Type: DEFAULT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.techniques ALTER COLUMN id SET DEFAULT nextval('public.techniques_id_seq'::regclass);


--
-- Name: topics id; Type: DEFAULT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.topics ALTER COLUMN id SET DEFAULT nextval('public.topics_id_seq'::regclass);


--
-- Name: blocks blocks_pkey; Type: CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.blocks
    ADD CONSTRAINT blocks_pkey PRIMARY KEY (id);


--
-- Name: chunks chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_pkey PRIMARY KEY (id);


--
-- Name: exam_questions exam_questions_pkey; Type: CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.exam_questions
    ADD CONSTRAINT exam_questions_pkey PRIMARY KEY (id);


--
-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);


--
-- Name: notes notes_pkey; Type: CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.notes
    ADD CONSTRAINT notes_pkey PRIMARY KEY (id);


--
-- Name: sources sources_pkey; Type: CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.sources
    ADD CONSTRAINT sources_pkey PRIMARY KEY (id);


--
-- Name: struggles struggles_pkey; Type: CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.struggles
    ADD CONSTRAINT struggles_pkey PRIMARY KEY (id);


--
-- Name: subjects subjects_pkey; Type: CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.subjects
    ADD CONSTRAINT subjects_pkey PRIMARY KEY (id);


--
-- Name: techniques techniques_pkey; Type: CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.techniques
    ADD CONSTRAINT techniques_pkey PRIMARY KEY (id);


--
-- Name: topic_chunks topic_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.topic_chunks
    ADD CONSTRAINT topic_chunks_pkey PRIMARY KEY (topic_id, chunk_id);


--
-- Name: topics topics_pkey; Type: CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.topics
    ADD CONSTRAINT topics_pkey PRIMARY KEY (id);


--
-- Name: chunks_embedding_idx; Type: INDEX; Schema: public; Owner: studyuser
--

CREATE INDEX chunks_embedding_idx ON public.chunks USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: sources_hash_idx; Type: INDEX; Schema: public; Owner: studyuser
--

CREATE UNIQUE INDEX sources_hash_idx ON public.sources USING btree (subject_id, content_hash);


--
-- Name: blocks blocks_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.blocks
    ADD CONSTRAINT blocks_subject_id_fkey FOREIGN KEY (subject_id) REFERENCES public.subjects(id);


--
-- Name: chunks chunks_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.sources(id);


--
-- Name: exam_questions exam_questions_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.exam_questions
    ADD CONSTRAINT exam_questions_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.sources(id);


--
-- Name: exam_questions exam_questions_topic_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.exam_questions
    ADD CONSTRAINT exam_questions_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.topics(id);


--
-- Name: jobs jobs_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.sources(id);


--
-- Name: notes notes_topic_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.notes
    ADD CONSTRAINT notes_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.topics(id);


--
-- Name: sources sources_block_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.sources
    ADD CONSTRAINT sources_block_id_fkey FOREIGN KEY (block_id) REFERENCES public.blocks(id);


--
-- Name: sources sources_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.sources
    ADD CONSTRAINT sources_subject_id_fkey FOREIGN KEY (subject_id) REFERENCES public.subjects(id);


--
-- Name: struggles struggles_topic_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.struggles
    ADD CONSTRAINT struggles_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.topics(id);


--
-- Name: techniques techniques_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.techniques
    ADD CONSTRAINT techniques_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.sources(id);


--
-- Name: techniques techniques_topic_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.techniques
    ADD CONSTRAINT techniques_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.topics(id);


--
-- Name: topic_chunks topic_chunks_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.topic_chunks
    ADD CONSTRAINT topic_chunks_chunk_id_fkey FOREIGN KEY (chunk_id) REFERENCES public.chunks(id);


--
-- Name: topic_chunks topic_chunks_topic_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.topic_chunks
    ADD CONSTRAINT topic_chunks_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.topics(id);


--
-- Name: topics topics_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.topics
    ADD CONSTRAINT topics_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.topics(id);


--
-- Name: topics topics_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: studyuser
--

ALTER TABLE ONLY public.topics
    ADD CONSTRAINT topics_subject_id_fkey FOREIGN KEY (subject_id) REFERENCES public.subjects(id);


--
-- PostgreSQL database dump complete
--

\unrestrict 50N0FpKGG9SGAuddjr1Hxm5bh9XhnqEeavoHGmO6RfA59iEAf3Nt2Qyvalr81kE

