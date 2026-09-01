import "@testing-library/jest-dom/vitest";

class EventSourceStub {
  static CLOSED = 2;
  readyState = EventSourceStub.CLOSED;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(_url: string) {}
  addEventListener() {}
  close() {}
}

Object.defineProperty(globalThis, "EventSource", { value: EventSourceStub, writable: true });

