# Hotel fault images

These are deliberately broken application images, not healthy deployment
defaults. Build them through `docker/images.hcl` and run `docker/test_image.sh`
on both platforms before publishing.

## Geo misconfiguration

`geo.Dockerfile` uses `yinfangchen/geo:app3`, pinned by its original image
digest. That image includes the complete Go source, vendored dependencies and
configuration. Its `GeoMongoAddress` is `mongodb-geo:27777`, while the database
listens on `27017`. Geo panics when the connection fails.

The AMD64 stage retains the original image; ARM64 rebuilds the embedded source
with the same Go 1.17.3 release and no dependency resolution. Both keep the
original configuration, including the incorrect port. The smoke test checks
the configuration and executes Geo to verify its expected startup panic.

## Correlated wrong-application rollout

The original `jackcuii/hotel-reservation:latest` image, inspected at
`sha256:b0ba6bd030c2579516ed03b67339cab8106577835f834cfe8acfb15189bdd6a2`,
actually contains Social Network's C++ services. None of Hotel's eight
entrypoint commands exists. Rolling it out to Hotel therefore fails before
application startup, with a missing executable error.

`correlated.Dockerfile` preserves that failure using SREGym's pinned multiarch
Social Network image. It does not claim binary identity with the old image:
the compatibility contract is the same wrong application and missing Hotel
commands. Build and smoke checks enforce that contract for all eight services.
