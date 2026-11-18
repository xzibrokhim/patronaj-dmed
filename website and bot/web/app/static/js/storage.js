const DB_NAME = "digital-bridge-prototype";
const DB_VERSION = 2;

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export function purgeLegacySensitiveStorage() {
  for (const key of ["db-patients-v1", "db-replies-v1"]) {
    localStorage.removeItem(key);
  }
}

export async function openDatabase() {
  const request = indexedDB.open(DB_NAME, DB_VERSION);
  request.onupgradeneeded = () => {
    const db = request.result;
    if (!db.objectStoreNames.contains("patients")) db.createObjectStore("patients", { keyPath: "local_id" });
    if (!db.objectStoreNames.contains("replies")) db.createObjectStore("replies", { keyPath: "id" });
    if (!db.objectStoreNames.contains("settings")) db.createObjectStore("settings", { keyPath: "key" });
  };
  return requestResult(request);
}

function transactionDone(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error);
  });
}

export async function getAll(db, storeName) {
  const transaction = db.transaction(storeName, "readonly");
  return requestResult(transaction.objectStore(storeName).getAll());
}

export async function putOne(db, storeName, value) {
  const transaction = db.transaction(storeName, "readwrite");
  transaction.objectStore(storeName).put(value);
  await transactionDone(transaction);
}

export async function putMany(db, storeName, values) {
  const transaction = db.transaction(storeName, "readwrite");
  const store = transaction.objectStore(storeName);
  values.forEach((value) => store.put(value));
  await transactionDone(transaction);
}

export async function getSetting(db, key, fallback = null) {
  const transaction = db.transaction("settings", "readonly");
  const result = await requestResult(transaction.objectStore("settings").get(key));
  return result?.value ?? fallback;
}

export async function setSetting(db, key, value) {
  return putOne(db, "settings", { key, value });
}
