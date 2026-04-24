# Manage Metadata

The metadata store holds arbitrary JSON dictionaries keyed by string keys. The service does not interpret the contents — it is a central location for any metadata that other services or users need to share.

## Store a metadata entry

```bash
curl -X POST http://localhost:8004/api/v1/metadata/sample_info \
  -H "Content-Type: application/json" \
  -d '{
    "value": {
      "sample_id": "NaCl-042",
      "composition": "NaCl",
      "holder": "capillary-1mm",
      "temperature_setpoint": 300,
      "notes": "pre-annealed at 500K for 2hr"
    }
  }'
```

Returns `201 Created`. Returns `409` if the key already exists — use PUT for upsert.

## Retrieve a metadata entry

```bash
curl http://localhost:8004/api/v1/metadata/sample_info
```

```json
{
    "key": "sample_info",
    "value": {
        "sample_id": "NaCl-042",
        "composition": "NaCl",
        "holder": "capillary-1mm",
        "temperature_setpoint": 300,
        "notes": "pre-annealed at 500K for 2hr"
    },
    "created_at": 1711468800.0,
    "updated_at": 1711468800.0
}
```

## List all metadata entries

```bash
curl http://localhost:8004/api/v1/metadata
```

Returns all entries sorted by key.

## Update a metadata entry (upsert)

PUT creates the entry if it does not exist, or replaces the value if it does.

```bash
curl -X PUT http://localhost:8004/api/v1/metadata/sample_info \
  -H "Content-Type: application/json" \
  -d '{
    "value": {
      "sample_id": "NaCl-042",
      "composition": "NaCl",
      "holder": "capillary-1mm",
      "temperature_setpoint": 350,
      "notes": "temperature increased to 350K"
    }
  }'
```

## Delete a metadata entry

```bash
curl -X DELETE http://localhost:8004/api/v1/metadata/sample_info
```

Returns `200` on success, `404` if the key does not exist.

## Persistence

Metadata entries are stored in the same SQLite database as the device registry. They survive service restarts.

## Example use cases

- **Sample metadata**: composition, holder, preparation notes — merged into Bluesky run start documents by the Experiment Execution Service
- **Operator info**: who is running the experiment, shift notes
- **Beamline state**: alignment parameters, current calibration values
- **Experiment configuration**: scan parameters, analysis settings shared between services
