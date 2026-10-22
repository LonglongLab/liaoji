-- Optional display metadata, separate from token identity and user pairing names.
CREATE TABLE device_client_info (
  device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
  computer_name TEXT NOT NULL,
  platform TEXT NOT NULL,
  client_name TEXT NOT NULL
);
