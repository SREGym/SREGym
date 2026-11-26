package airport.meal;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.Properties;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicBoolean;

class MealBridge {
    static final String INPUT = "allowances";
    static final String OUTPUT = "issue-orders";
    static final String GROUP = "meal-issuer";
    static final Duration POLL = Duration.ofSeconds(1);
    static final ObjectMapper JSON = new ObjectMapper();
    static final AtomicBoolean STOPPING = new AtomicBoolean();
    private static final Path READY = Path.of("/tmp/meal-bridge-ready");
    private static final HttpClient HTTP = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();

    public static void main(String[] args) throws Exception {
        CountDownLatch finished = new CountDownLatch(1);
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            STOPPING.set(true);
            try {
                finished.await();
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            }
        }));
        try {
            Files.deleteIfExists(READY);
            switch (args[0]) {
                case "dispatch":
                    Dispatch.run();
                    break;
                case "issuer":
                    Issuer.run();
                    break;
                default:
                    throw new IllegalArgumentException("Expected dispatch or issuer");
            }
        } finally {
            finished.countDown();
        }
    }

    static Properties brokerSettings() {
        Properties settings = new Properties();
        settings.setProperty("bootstrap.servers", System.getenv().getOrDefault("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"));
        settings.setProperty("request.timeout.ms", "10000");
        settings.setProperty("default.api.timeout.ms", "15000");
        return settings;
    }

    static Properties consumerSettings() {
        Properties settings = brokerSettings();
        settings.setProperty("key.deserializer", "org.apache.kafka.common.serialization.StringDeserializer");
        settings.setProperty("value.deserializer", "org.apache.kafka.common.serialization.StringDeserializer");
        settings.setProperty("enable.auto.commit", "false");
        settings.setProperty("isolation.level", "read_committed");
        settings.setProperty("allow.auto.create.topics", "false");
        return settings;
    }

    static void ready() throws IOException {
        if (!Files.exists(READY)) {
            Files.createFile(READY);
        }
    }

    static String text(JsonNode row, String field) {
        return row.required(field).asText();
    }

    static String path(String collection, String... parts) {
        StringBuilder path = new StringBuilder("/").append(collection);
        for (String part : parts) {
            path.append('/').append(URLEncoder.encode(part, StandardCharsets.UTF_8).replace("+", "%20"));
        }
        return path.toString();
    }

    static JsonNode get(String partner, String path) throws IOException, InterruptedException {
        return request(partner, path, null);
    }

    static JsonNode post(String partner, String path, ObjectNode body) throws IOException, InterruptedException {
        return request(partner, path, body);
    }

    private static JsonNode request(String partner, String path, ObjectNode body)
            throws IOException, InterruptedException {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create("http://" + partner + ":8080" + path))
                .timeout(Duration.ofSeconds(5));
        if (body != null) {
            request.header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body)));
        }
        HttpResponse<String> response = HTTP.send(request.build(), HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 200) {
            throw new PartnerError(partner, path, response.statusCode(), response.body());
        }
        return JSON.readTree(response.body());
    }

    static final class PartnerError extends IOException {
        final int status;

        PartnerError(String partner, String path, int status, String body) {
            super(partner + path + " returned HTTP " + status + " " + body);
            this.status = status;
        }
    }
}
