import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.clients.producer.ProducerConfig;
import org.apache.kafka.common.serialization.StringSerializer;
import org.apache.kafka.common.serialization.ByteArraySerializer;
import java.util.Properties;
import java.util.concurrent.atomic.AtomicLong;

public class ProducerLeak {

    static AtomicLong totalCount = new AtomicLong(0);

    public static void main(String[] args) throws InterruptedException {
        int numThreads = Integer.parseInt(System.getenv().getOrDefault("NUM_THREADS", "20"));

        List<Thread> threads = new ArrayList<>();

        for (int i = 0; i < numThreads; i++) {
            int threadId = i;
            Thread t = new Thread(() -> runLoop(threadId));
            t.start();
            threads.add(t)
        }

        for (Thread t : threads) {
            t.join();
        }
    }

    static void runLoop(int threadId) {
        while (true) {
            try {
                Properties props = new Properties();
                props.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, "kafka:9092");
                props.put(ProducerConfig.ENABLE_IDEMPOTENCE_CONFIG, true);
                props.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class.getName());
                props.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, ByteArraySerializer.class.getName());

                KafkaProducer<String, byte[]> producer = new KafkaProducer<>(props);
                producer.send(new ProducerRecord<>("orders", "order_created".getBytes()));

                long count = totalCount.incrementAndGet();
                if (count % 1000 == 0) {
                    System.out.println("Created " + count + " producers total");
                }
            } catch (Exception e) {
                System.out.println("Thread " + threadId + " error: " + e.getMessage());
            }
        }
    }
}