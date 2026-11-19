Add S3-compatible object storage for uploaded images to this charm, through an `s3` integration with `s3-integrator` (interface `s3`, at most one relation), using the `data_platform_libs` `s3` charm library (charms.data_platform_libs.v0.s3) and requesting the bucket `hedgedoc`. Requirements:

- When the relation has supplied credentials, HedgeDoc stores uploads in S3: `CMD_IMAGE_UPLOAD_TYPE` is `s3`, overriding the `image-upload-type` option, and `CMD_S3_ACCESS_KEY_ID`, `CMD_S3_SECRET_ACCESS_KEY` and `CMD_S3_BUCKET` come from the relation, with `CMD_S3_REGION`, `CMD_S3_ENDPOINT` and `CMD_S3_FOLDER` (the relation's `path`, defaulting to `uploads`) when they're available. Without the relation, uploads follow `image-upload-type` as now.
- An `s3` relation that hasn't delivered credentials yet is not the same as no relation: don't push a configuration that writes uploads to the local filesystem in the meantime (they'd be invisible once the credentials arrive). When the credentials arrive, configure object storage.
- Keep the existing unit tests passing, and add unit tests for the new behaviour.
- Run the linters and fix anything they report.

This charm already integrates with several other charms; follow its existing structure and conventions. Work in this directory. Do not ask questions; make reasonable decisions and finish the task.
