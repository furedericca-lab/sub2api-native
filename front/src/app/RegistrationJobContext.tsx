import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type PropsWithChildren } from "react";

import { api, type JobStatus } from "@/lib/api";

type RegistrationJobContextValue = {
  job: JobStatus | null;
  replaceJob: (job: JobStatus) => void;
  beginJobObservation: () => number;
  acceptJobObservation: (observation: number, job: JobStatus) => boolean;
};

export type RegistrationJobState = "loading" | "running" | "idle";

const RegistrationJobContext = createContext<RegistrationJobContextValue | null>(null);
const RegistrationJobStateContext = createContext<RegistrationJobState>("loading");

const jobStatusFields: ReadonlyArray<keyof JobStatus> = [
  "running",
  "started_at",
  "finished_at",
  "target_count",
  "workers",
  "source",
  "last_error",
  "log_count",
  "latest_log_id",
  "completed_count",
  "success_count",
  "failure_count",
  "progress_percent",
  "current_stage",
  "current_email",
  "batch_id",
  "profile_id",
  "profile_name",
];

function sameJobStatus(current: JobStatus | null, next: JobStatus) {
  return !!current && jobStatusFields.every((field) => current[field] === next[field]);
}

export function RegistrationJobProvider({ children }: PropsWithChildren) {
  const [job, setJob] = useState<JobStatus | null>(null);
  const jobRevision = useRef(0);

  const commitJob = useCallback((next: JobStatus, observation?: number) => {
    if (observation != null && observation !== jobRevision.current) return false;
    jobRevision.current += 1;
    setJob((current) => (sameJobStatus(current, next) ? current : next));
    return true;
  }, []);

  const replaceJob = useCallback((next: JobStatus) => {
    commitJob(next);
  }, [commitJob]);

  const beginJobObservation = useCallback(() => jobRevision.current, []);
  const acceptJobObservation = useCallback(
    (observation: number, next: JobStatus) => commitJob(next, observation),
    [commitJob]
  );

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const loadInitialJob = async () => {
      const observation = beginJobObservation();
      try {
        const data = await api.job();
        if (!cancelled) acceptJobObservation(observation, data.job);
      } catch {
        if (!cancelled) timer = window.setTimeout(loadInitialJob, 5000);
      }
    };

    void loadInitialJob();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [acceptJobObservation, beginJobObservation]);

  useEffect(() => {
    if (!job?.running) return;
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      const observation = beginJobObservation();
      try {
        const data = await api.job();
        if (cancelled) return;
        const accepted = acceptJobObservation(observation, data.job);
        if (accepted && !data.job.running) return;
        timer = window.setTimeout(poll, 3000);
      } catch {
        if (cancelled) return;
        timer = window.setTimeout(poll, 5000);
      }
    };

    timer = window.setTimeout(poll, 3000);
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [acceptJobObservation, beginJobObservation, job?.running]);

  const value = useMemo(
    () => ({ job, replaceJob, beginJobObservation, acceptJobObservation }),
    [acceptJobObservation, beginJobObservation, job, replaceJob]
  );
  const state: RegistrationJobState = job === null ? "loading" : job.running ? "running" : "idle";
  return (
    <RegistrationJobContext.Provider value={value}>
      <RegistrationJobStateContext.Provider value={state}>
        {children}
      </RegistrationJobStateContext.Provider>
    </RegistrationJobContext.Provider>
  );
}

export function useRegistrationJob() {
  const context = useContext(RegistrationJobContext);
  if (!context) throw new Error("useRegistrationJob must be used inside RegistrationJobProvider");
  return context;
}

export function useRegistrationJobRunning() {
  return useRegistrationJobState() === "running";
}

export function useRegistrationJobState() {
  return useContext(RegistrationJobStateContext);
}
